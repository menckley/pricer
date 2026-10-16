"""Construction du prix d'un deal : couverture EEX, résidu day-ahead, risque, frais, financement, marge.

Prix = prix juste ± (risque + frais + financement + marge) / volume, avec + pour une vente au client
(on lui facture nos coûts) et − pour un achat à une contrepartie (on les déduit du prix payé).
Le financement ne s'applique qu'aux ventes physiques : sourcing payé à J+`sourcing_jours`, client à
J+`client_jours`, comptés depuis le premier jour livré de chaque mois, au taux €STR + spread.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from pricer import rates
from pricer.hedging import LISTED, STRATEGIES, Hedge, hedge, hedge_summary, listed_contract, listed_hedge
from pricer.market import Market
from pricer.products import Product, require_on_grid
from pricer.risk import RiskResult, history_bounds, residual_risk
from pricer.settings import Settings


@dataclass(frozen=True)
class Margin:
    eur: float
    rule: str                        # « rules » ou « manual »
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Quote:
    product: Product
    side: str
    volume: float                    # MWh, valeur absolue
    fair_price: float
    price: float                     # prix proposé, EUR/MWh
    lines: pd.DataFrame              # poste, EUR, EUR/MWh
    hedge: Hedge
    hedge_values: dict
    risk: RiskResult
    financing: pd.DataFrame
    margin: Margin
    strategies: pd.DataFrame
    context: dict
    warnings: list = field(default_factory=list)


def margin_by_rules(s: Settings, volume: float, shape_value: float, years: float) -> Margin:
    variable = volume * (s.margin_base + s.margin_per_year * max(0.0, years - 1.0))
    shape = s.margin_shape_pct / 100 * abs(shape_value)
    total = max(variable + shape, s.margin_floor)
    return Margin(total, "rules", {"base + tenor €": variable, "shape €": shape, "floor €": s.margin_floor,
                                   "floor applied": total > variable + shape})


def financing_cost(mkt: Market, p: Product, s: Settings, rate_pct: float) -> pd.DataFrame:
    """Par mois livré : coût de sourcing attendu, payé à J+sourcing, encaissé à J+client, et son coût de portage."""
    q = p.mw
    cost = -(q * mkt.price[q.index])                      # vente : q < 0, coût de sourcing > 0
    first = mkt.day[q.index].groupby(mkt.month[q.index]).min()
    m = cost.groupby(mkt.month[q.index]).sum().to_frame("sourcing cost €")
    m["first delivery day"] = first
    m["sourcing paid"] = [rates.offset(d, s.sourcing_days) for d in first]
    m["client pays"] = [rates.offset(d, s.client_days) for d in first]
    m["days funded"] = s.client_days - s.sourcing_days
    m["financing cost €"] = m["sourcing cost €"] * rate_pct / 100 * m["days funded"] / s.day_count
    m.index.name = "month"
    return m


def _years(mkt: Market, p: Product) -> float:
    d = mkt.day[p.mw.index]
    return ((d.max() - d.min()).days + 1) / 365.25


def max_contract_mw(h: Hedge) -> float:
    """Plus gros volume horaire traité sur un seul contrat EEX (Base, ou Base + Peak aux heures peak)."""
    t = h.table
    if t.empty:
        return 0.0
    if "MW" in t.columns:                                  # contrat coté couvert par lui-même
        return float(t["MW"].abs().max())
    return float(pd.concat([t["Base MW"].abs(), (t["Base MW"] + t["Peak MW"]).abs()], axis=1).max(axis=1).max())


def _da_mwh(hv: dict) -> float:
    return hv["day-ahead surplus to sell MWh"] + hv["day-ahead shortfall to buy MWh"]


def quote(mkt: Market, p: Product, s: Settings, strategy: str | None = None, spot: pd.Series | None = None,
          manual_margin_eur_mwh: float | None = None, manual_reason: str = "") -> Quote:
    """Prix d'un deal. `strategy` : « auto » (défaut du config) ou une clé de STRATEGIES.
    En « auto », le scénario retenu est le moins cher (risque, frais, conversion, liquidité, marge) ;
    à égalité, celui qui laisse le moins de MWh en day-ahead, puis le moins de contrats EEX.
    Prime de liquidité : `eur_mwh_par_mw` × (MW max traités sur un contrat − `seuil_mw`), sur les MWh couverts EEX."""
    require_on_grid(mkt, p)
    if p.mixed:
        raise ValueError("produit mixte achat / vente : coter chaque sens séparément (un prix par MWh n'a pas de sens)")
    if manual_margin_eur_mwh is not None and not manual_reason.strip():
        raise ValueError("marge manuelle : un motif est obligatoire")
    side = p.side
    need_fin = p.physical and side == "sell"
    missing = s.missing(need_fin, manual_margin_eur_mwh is not None, p.physical)
    if missing:
        raise LookupError("paramètres à renseigner avant de pricer : " + ", ".join(missing))
    strategy = strategy or s.strategy
    if strategy != "auto" and strategy not in STRATEGIES:
        raise ValueError(f"stratégie inconnue {strategy!r} : auto, {', '.join(STRATEGIES)}")
    if spot is None:
        spot = mkt.spot(*history_bounds(mkt, s.history_start))
    V, sq = p.abs_volume, p.volume
    direction = 1.0 if side == "sell" else -1.0

    if need_fin:
        d_estr, v_estr = rates.estr(mkt.pricing_date)
        rate = v_estr + s.spread_bp / 100
        fin = financing_cost(mkt, p, s, rate)
        fin_eur = float(fin["financing cost €"].sum())
        rate_ctx = {"€STR date": str(d_estr), "€STR %": v_estr, "spread bp": s.spread_bp, "funding rate %": rate}
    else:
        fin, fin_eur, rate_ctx = pd.DataFrame(), 0.0, {}
    if manual_margin_eur_mwh is not None and manual_margin_eur_mwh < 0:
        raise ValueError("marge manuelle négative")
    years = _years(mkt, p)

    listed = listed_contract(mkt, p)
    if listed is not None:
        # produit coté : couvert par lui-même, aucune stratégie à choisir
        h = listed_hedge(mkt, p, listed)
        hv = hedge_summary(mkt, p, h)
        # prix juste = settlement du contrat lui-même (pas la moyenne de la courbe, arrondie au centime près)
        hv = {**hv, "curve value €": hv["EEX hedge at settlement €"], "settlement rounding €": 0.0}
        rk = RiskResult(0.0, 0.0, s.confidence, s.correlation,
                        pd.DataFrame(columns=["σ €", "mean €", "samples"]), pd.DataFrame())
        candidates = {LISTED: (h, hv, rk)}
    else:
        candidates = {}
        for strat in STRATEGIES:
            h = hedge(mkt, p, strat)
            hv = hedge_summary(mkt, p, h)
            candidates[strat] = (h, hv, residual_risk(mkt, h, spot, s.confidence, s.window, s.correlation))

    evaluated, comp = {}, []
    for name, (h, hv, rk) in candidates.items():
        fees = s.eex_fee * hv["EEX hedged MWh"] + s.epex_fee * _da_mwh(hv)
        conv = s.conversion_fee * V if p.physical else 0.0          # tous les MWh livrés physiquement
        mw_max = max_contract_mw(h)
        liq_rate = s.liquidity_eur_mwh_per_mw * max(0.0, mw_max - s.liquidity_threshold_mw)
        liq = liq_rate * hv["EEX hedged MWh"]
        if manual_margin_eur_mwh is not None:
            mg = Margin(manual_margin_eur_mwh * V, "manual", {"€/MWh": manual_margin_eur_mwh, "reason": manual_reason})
        else:
            mg = margin_by_rules(s, V, hv["residual at HPFC €"], years)
        costs = rk.cost + fees + conv + liq + mg.eur
        evaluated[name] = (h, hv, rk, fees, conv, liq, mg)
        comp.append({"strategy": name, "EEX contracts": hv["EEX contracts"], "EEX hedged MWh": hv["EEX hedged MWh"],
                     "day-ahead MWh": _da_mwh(hv), "max MW per contract": mw_max, "risk cost €": rk.cost,
                     "fees + conversion €": fees + conv, "liquidity €": liq, "margin €": mg.eur,
                     "offer price €/MWh": (hv["curve value €"] / sq) + direction * (costs + fin_eur) / V})
    strategies = pd.DataFrame(comp).set_index("strategy")
    if listed is not None:
        chosen = LISTED
    elif strategy == "auto":
        # le scénario gagnant est le moins cher : coûts les plus bas (risque, frais, conversion, liquidité, marge) ;
        # à égalité, le moins de MWh en day-ahead puis le moins de contrats
        costs_eur = strategies["risk cost €"] + strategies["fees + conversion €"] + strategies["liquidity €"] \
            + strategies["margin €"]
        order = pd.DataFrame({"c": costs_eur.round(6), "da": strategies["day-ahead MWh"].round(6),
                              "n": strategies["EEX contracts"]})
        chosen = order.sort_values(["c", "da", "n"]).index[0]
    else:
        chosen = strategy
    strategies["selected"] = strategies.index == chosen
    if listed is not None:
        strategies = pd.DataFrame()
    h, hv, rk, fees, conv, liq, mg = evaluated[chosen]
    fair = hv["curve value €"] / sq

    adds = rk.cost + fees + conv + liq + fin_eur + mg.eur
    price = fair + direction * adds / V
    sign = "+" if direction > 0 else "−"
    rows = [("EEX hedge at settlement", hv["EEX hedge at settlement €"] / sq * V),
            ("Day-ahead residual at HPFC (shape premium)", hv["residual at HPFC €"] / sq * V),
            ("Settlement rounding", hv["settlement rounding €"] / sq * V),
            ("= Fair price", fair * V),
            (f"{sign} Residual risk cost ({rk.confidence:.0%})", direction * rk.cost),
            (f"{sign} EEX and day-ahead fees", direction * fees),
            (f"{sign} Financial-to-physical conversion", direction * conv),
            (f"{sign} Liquidity premium", direction * liq),
            (f"{sign} Funding cost", direction * fin_eur),
            (f"{sign} Margin ({mg.rule})", direction * mg.eur),
            ("= Offer price", price * V)]
    lines = pd.DataFrame(rows, columns=["item", "€"]).set_index("item")
    lines["€/MWh"] = lines["€"] / V

    warnings = []
    unhedge = hv["unhedgeable MWh (no quoted month/quarter/year)"]
    if unhedge > 0:
        warnings.append(f"{unhedge:,.0f} MWh sourced in day-ahead without any EEX hedge (current month): "
                        "their price-level risk versus the HPFC is NOT included in the risk cost")
    if chosen != LISTED and STRATEGIES[chosen]["levels"][0] != "mois" and _da_mwh(hv) > 1e-6:
        warnings.append("quarterly hedge: the risk of each month versus its quarter is NOT included in the risk cost")

    context = {"market": mkt.name, "pricing date": str(mkt.pricing_date), "strategy requested": strategy,
               "strategy selected": chosen,
               "spot history": f"{spot.index[0].tz_convert(mkt.tz).date()} → {spot.index[-1].tz_convert(mkt.tz).date()}",
               "confidence": s.confidence, "seasonal window ± months": s.window, "month correlation": s.correlation,
               "EEX fee €/MWh": s.eex_fee, "day-ahead fee €/MWh": s.epex_fee,
               "fin-to-phys conversion €/MWh": s.conversion_fee if p.physical else None,
               "liquidity threshold MW": s.liquidity_threshold_mw, "liquidity €/MWh per MW": s.liquidity_eur_mwh_per_mw,
               "sourcing paid D+": s.sourcing_days, "client pays D+": s.client_days,
               "day count": s.day_count, **rate_ctx,
               "pricer config": s.hash_pricer, "margin config": s.hash_margin}
    return Quote(p, side, V, fair, price, lines, h, hv, rk, fin, mg, strategies, context, warnings)
