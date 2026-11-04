"""Pricing d'options EEX sur futures Base France : call, put, collar (cap + floor), au format du journal.

Le prix est donné du point de vue du client, par MWh du future sous-jacent :
  - fair premium = Σ position client × Black-76(F, K, T, σ implicite EEX) ;
  - prime de volatilité : chaque jambe que nous vendons est valorisée à σ + `prime_vol_points`, chaque jambe que
    nous achetons à σ − `prime_vol_points` (nous ne pourrons pas forcément nous couvrir à l'écran) ;
  - frais : `frais_eur_mwh` × MWh sous-jacent × nombre de jambes ; marge : section `options` de marge.yaml ;
  - vente au client : prime proposée = prime à la volatilité ajustée + (frais + marge) / MWh ;
    achat à une contrepartie : − (frais + marge) / MWh.
Comparaison : la même structure valorisée à la volatilité historique du future (si l'archive est assez longue),
écart de prime et de volatilité ; ce n'est pas une mesure de précision (aucune des deux n'est « la vérité »).
"""
from __future__ import annotations

import pandas as pd

from pricer.hedging import Hedge
from pricer.market import Market
from pricer.options import black76, build_smile, greeks, historical_vol, last_trading_day
from pricer.products import Product, eex
from pricer.quote import Margin, Quote
from pricer.risk import RiskResult
from pricer.settings import Settings

OPTION = "EEX option (Black-76)"
# Jambes du point de vue du client : (type, position client, strike utilisé)
STRUCTURES = {
    "call": [("call", +1, "strike")],                 # cap
    "put": [("put", +1, "strike")],                   # floor
    "collar": [("call", +1, "strike_high"), ("put", -1, "strike_low")],   # le client achète le cap, vend le floor
}
LABELS = {"call": "Call (cap)", "put": "Put (floor)", "collar": "Collar (long cap / short floor)"}
VOL_SOURCES = {"implicite": "EEX implied", "historique": "historical", "prudente": "prudent (max sold / min bought)"}


def option_series(mkt: Market) -> pd.DataFrame:
    """Futures Base mois / trimestre / an de la séance qui ont des settlements d'options archivés ce jour-là."""
    opts = mkt.options()
    names = set(opts.loc[opts["session"] == mkt.pricing_date, "name"])
    st = mkt.settle[(mkt.settle["profile"] == "Base") & mkt.settle["kind"].isin(["month", "quarter", "year"])]
    st = st[st["name"].isin(names)].copy()
    st["expiry"] = [last_trading_day(r.kind, r.start) for r in st.itertuples()]
    return st[st["expiry"] > mkt.pricing_date].sort_values(["start", "kind"]).reset_index(drop=True)


def quote_option(mkt: Market, structure: str, series: str, mw: float, side: str, s: Settings,
                 strike: float | None = None, strike_low: float | None = None, strike_high: float | None = None,
                 manual_margin_eur_mwh: float | None = None, manual_reason: str = "",
                 vol_source: str | None = None) -> Quote:
    """`vol_source` (défaut : options.volatilite_prime) : volatilité de la prime proposée — « implicite »
    (settlements d'options EEX), « historique » (settlements du future ; refusée si l'archive est trop courte) ou
    « prudente » (jambe vendue : la plus haute des deux, jambe achetée : la plus basse). Le prix juste affiché
    reste toujours celui à la volatilité implicite EEX."""
    if structure not in STRUCTURES:
        raise ValueError(f"structure inconnue {structure!r} : {', '.join(STRUCTURES)}")
    if side not in ("sell", "buy"):
        raise ValueError("side : sell (au client) ou buy (à une contrepartie)")
    if manual_margin_eur_mwh is not None and (manual_margin_eur_mwh < 0 or not manual_reason.strip()):
        raise ValueError("marge manuelle : valeur positive et motif obligatoire")
    missing = s.missing_option(manual_margin_eur_mwh is not None)
    if missing:
        raise LookupError("paramètres à renseigner avant de pricer : " + ", ".join(missing))
    strikes = {"strike": strike, "strike_low": strike_low, "strike_high": strike_high}
    for _, _, key in STRUCTURES[structure]:
        if strikes[key] is None or strikes[key] <= 0:
            raise ValueError(f"{key} : strike positif requis pour un {structure}")
    if structure == "collar" and not strike_low < strike_high:
        raise ValueError("collar : strike du floor inférieur au strike du cap")

    row = mkt.settle[(mkt.settle["profile"] == "Base") & (mkt.settle["name"] == series)]
    if row.empty:
        raise LookupError(f"future Base {series} non coté à la séance {mkt.pricing_date}")
    r = row.iloc[0]
    if r["kind"] not in ("month", "quarter", "year"):
        raise ValueError(f"pas d'option EEX sur un produit {r['kind']}")
    opts = mkt.options()
    sessions = sorted(opts.loc[opts["name"] == series, "session"].unique())
    expiry = last_trading_day(r["kind"], r["start"])
    late = [d for d in sessions if d > expiry]
    if late:
        raise ValueError(f"règle d'échéance contredite : settlements {series} archivés après le {expiry} ({late[0]})")
    prev = [d for d in sessions if d < mkt.pricing_date]
    previous = opts[(opts["name"] == series) & (opts["session"] == prev[-1])] if prev else None
    smile = build_smile(opts, series, r["kind"], r["start"], r["end"], mkt.pricing_date, float(r["settlement"]),
                        previous)
    hist = historical_vol(mkt.futures_history(), series, mkt.pricing_date, int(s.option_min_returns))

    F, T = smile.forward, smile.T
    we = -1 if side == "sell" else +1                 # notre position = − position client si on vend la structure
    vol_source = vol_source or s.option_vol_source
    if vol_source not in VOL_SOURCES:
        raise ValueError(f"volatilité de la prime inconnue {vol_source!r} : {', '.join(VOL_SOURCES)}")
    if vol_source == "historique" and hist["sigma"] is None:
        raise LookupError(f"volatilité historique non disponible pour {series} : {hist['reason']}")
    vp = s.option_vol_premium_pts / 100
    legs, warnings = [], list(smile.warnings)
    for kind, client_pos, key in STRUCTURES[structure]:
        K, call = float(strikes[key]), kind == "call"
        vol, source = smile.vol_at(K)
        if source.startswith("flat"):
            warnings.append(f"{kind} K={K:g}: volatility by {source}")
        ours = we * client_pos
        hs = hist["sigma"]
        if vol_source == "historique":
            base_vol = hs
        elif vol_source == "prudente" and hs is not None:
            base_vol = max(vol, hs) if ours < 0 else min(vol, hs)
        else:
            base_vol = vol
        vol_adj = base_vol + vp if ours < 0 else max(base_vol - vp, 1e-6)
        px = black76(F, K, T, vol, call)
        g = greeks(F, K, T, vol, call)
        leg = {"leg": f"{kind} {K:g}", "type": kind, "strike": K, "client position": client_pos,
               "our position": ours, "EEX vol %": 100 * vol, "vol source": source, "premium EEX vol €/MWh": px,
               "vol basis %": 100 * base_vol, "premium vol basis €/MWh": black76(F, K, T, base_vol, call),
               "vol charged %": 100 * vol_adj, "premium charged €/MWh": black76(F, K, T, vol_adj, call),
               **{f"{k} (client)": client_pos * v for k, v in g.items()}}
        if hist["sigma"] is not None:
            leg.update({"historical vol %": 100 * hist["sigma"],
                        "premium historical vol €/MWh": black76(F, K, T, hist["sigma"], call)})
        legs.append(leg)
    legs = pd.DataFrame(legs).set_index("leg")

    p0 = eex(mkt, series, "Base", mw, side)
    n_legs = len(legs)
    name = (f"{'Sell' if side == 'sell' else 'Buy'} {LABELS[structure]} {series} "
            + (f"K={strike:g}" if structure != "collar" else f"floor {strike_low:g} / cap {strike_high:g}")
            + f" — {mw:g} MW")
    product = Product(name, p0.mw, False, {"type": "option", "structure": structure, "series": series, "mw": mw,
                                           "strikes": {k: v for k, v in strikes.items() if v is not None}})
    V = p0.abs_volume
    fair = float((legs["client position"] * legs["premium EEX vol €/MWh"]).sum())
    basis = float((legs["client position"] * legs["premium vol basis €/MWh"]).sum())
    charged = float((legs["client position"] * legs["premium charged €/MWh"]).sum())
    fees = s.option_fee * V * n_legs
    if manual_margin_eur_mwh is not None:
        mg = Margin(manual_margin_eur_mwh * V, "manual", {"€/MWh": manual_margin_eur_mwh, "reason": manual_reason})
    else:
        total = max(s.margin_option_base * V, s.margin_option_floor)
        mg = Margin(total, "rules (options)", {"base €": s.margin_option_base * V, "floor €": s.margin_option_floor,
                                               "floor applied": total > s.margin_option_base * V})
    direction = 1.0 if side == "sell" else -1.0
    price = charged + direction * (fees + mg.eur) / V
    sign = "+" if direction > 0 else "−"
    rows = [("Premium at EEX settlement volatility (fair)", fair * V),
            (f"Volatility basis: {VOL_SOURCES[vol_source]}", (basis - fair) * V),
            (f"{sign} Volatility premium ({s.option_vol_premium_pts:g} vol pts)", (charged - basis) * V),
            (f"{sign} EEX option fees", direction * fees), (f"{sign} Margin ({mg.rule})", direction * mg.eur),
            ("= Offer premium (paid by the client if positive)", price * V)]
    lines = pd.DataFrame(rows, columns=["item", "€"]).set_index("item")
    lines["€/MWh"] = lines["€"] / V

    comparison = {"premium EEX vol €/MWh": fair}
    if hist["sigma"] is not None:
        ph = float((legs["client position"] * legs["premium historical vol €/MWh"]).sum())
        comparison.update({"premium historical vol €/MWh": ph, "difference €/MWh": fair - ph,
                           "difference %": 100 * (fair - ph) / abs(ph) if abs(ph) > 1e-9 else None,
                           "vol difference pts (per leg)": list(legs["EEX vol %"] - 100 * hist["sigma"])})
    else:
        comparison["historical vol"] = f"not available: {hist['reason']}"
    hedge = Hedge(OPTION, pd.DataFrame(), p0.mw.reindex(mkt.curve.index, fill_value=0.0) * 0.0,
                  p0.mw.reindex(mkt.curve.index, fill_value=0.0) * 0.0, pd.Series(True, index=mkt.curve.index))
    risk = RiskResult(0.0, 0.0, s.confidence, s.correlation, pd.DataFrame(columns=["σ €", "mean €", "samples"]),
                      pd.DataFrame())
    option = {"structure": structure, "series": series, "expiry": str(smile.expiry), "T years": T,
              "forward €/MWh": F, "legs": legs, "smile": smile.table, "checks": smile.checks,
              "historical": hist, "comparison": comparison,
              "formula": f"{name}: {price:,.3f} €/MWh ({price * V:,.0f} €)"}
    context = {"market": mkt.name, "pricing date": str(mkt.pricing_date), "pricing": OPTION,
               "strategy requested": OPTION, "strategy selected": OPTION, "expiry": str(smile.expiry),
               "volatility": "implied from EEX option settlements of the pricing session",
               "volatility of the offer premium": VOL_SOURCES[vol_source],
               "vol premium pts": s.option_vol_premium_pts, "option fee €/MWh": s.option_fee,
               "pricer config": s.hash_pricer, "margin config": s.hash_margin}
    return Quote(product, side, V, fair, price, lines, hedge, {}, risk, pd.DataFrame(), mg, pd.DataFrame(),
                 context, warnings, None, None, None, option)


def underlying_hours(mkt: Market, series: str) -> int:
    r = mkt.settle[(mkt.settle["profile"] == "Base") & (mkt.settle["name"] == series)].iloc[0]
    return int(mkt.days(r["start"], r["end"]).sum())

