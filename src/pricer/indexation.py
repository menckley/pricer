"""Pricing d'un contrat physique indexé sur le spot EPEX day-ahead : « EPEX spot + spread ».

Convention (celle des contrats de fourniture indexés) :
  index du mois m = moyenne du spot day-ahead sur les heures livrées du mois, pondérée par les MWh de chaque heure
                    (n'importe quel profil : bloc, grille, courbe horaire), arrondie au centime SUPÉRIEUR ;
  prix facturé du mois m = index du mois m + spread (vente au client) ou − spread (achat à un producteur).

Le fournisseur achète (ou revend) chaque heure livrée en day-ahead au prix spot : la partie énergie est couverte par
construction, sans risque de prix ni de forme, sans contrat EEX. Le spread couvre donc des coûts, pas un risque :
  frais day-ahead × MWh, conversion financier → physique × MWh, financement (ventes : sourcing payé à J+sourcing,
  client à J+client, sur la valeur de l'énergie à la HPFC), marge (règles « indexation » de marge.yaml ou manuelle),
  et, pour un achat, l'arrondi au centime supérieur de l'index, payé au producteur (0,01 €/MWh au plus, compté en
  entier par prudence ; pour une vente, ce gain n'est pas déduit).
Spread proposé = spread exact arrondi au centime supérieur.
L'index attendu à la HPFC est affiché pour information (prix « tout compris » estimé) : il n'entre pas dans le
spread.
"""
from __future__ import annotations

import math

import pandas as pd

from pricer import rates
from pricer.hedging import full_da_hedge, hedge_summary
from pricer.market import Market
from pricer.products import Product, require_on_grid
from pricer.quote import Margin, Quote, _years, financing_cost
from pricer.risk import RiskResult
from pricer.settings import Settings

INDEX = "EPEX spot indexed"
ROUNDING_MAX = 0.01                     # €/MWh : écart maximal de l'arrondi au centime supérieur


def ceil_cent(x: float) -> float:
    """Arrondi au centime supérieur, sans erreur de virgule flottante (0,69 reste 0,69)."""
    return math.ceil(round(x * 100, 6)) / 100


def monthly_index(mkt: Market, p: Product, prices: pd.Series) -> pd.DataFrame:
    """Index de chaque mois livré : moyenne de `prices` sur les heures livrées, pondérée par les MWh, arrondie au
    centime supérieur. `prices` : HPFC (index attendu) ou spot réalisé (index de facturation)."""
    q = p.mw.abs()
    px = prices.reindex(q.index)
    if px.isna().any():
        raise LookupError(f"prix manquant pour {int(px.isna().sum())} heure(s) livrée(s)")
    month = mkt.month[q.index]
    df = pd.DataFrame({"MWh": q, "value": q * px, "month": month}).groupby("month").sum()
    df["index exact €/MWh"] = df["value"] / df["MWh"]
    df["index €/MWh (ceil to cent)"] = [ceil_cent(x) for x in df["index exact €/MWh"]]
    return df.drop(columns="value")


def margin_indexed(s: Settings, volume: float, years: float) -> Margin:
    variable = volume * (s.margin_index_base + s.margin_index_per_year * max(0.0, years - 1.0))
    total = max(variable, s.margin_index_floor)
    return Margin(total, "rules (indexation)", {"base + tenor €": variable, "floor €": s.margin_index_floor,
                                                "floor applied": total > variable})


def quote_indexed(mkt: Market, p: Product, s: Settings, manual_margin_eur_mwh: float | None = None,
                  manual_reason: str = "") -> Quote:
    """Spread d'un contrat physique « EPEX spot ± spread ». Le prix du Quote est l'estimation tout compris
    (index attendu à la HPFC ± spread proposé) ; le spread est dans `q.indexation`."""
    require_on_grid(mkt, p)
    if p.mixed:
        raise ValueError("produit mixte achat / vente : coter chaque sens séparément")
    if not p.physical:
        raise ValueError("indexation spot : livraison physique uniquement (un swap « spot contre spot + spread » "
                         "se réduit au spread, sans livraison ni coût de sourcing)")
    if manual_margin_eur_mwh is not None and not manual_reason.strip():
        raise ValueError("marge manuelle : un motif est obligatoire")
    if manual_margin_eur_mwh is not None and manual_margin_eur_mwh < 0:
        raise ValueError("marge manuelle négative")
    side = p.side
    need_fin = side == "sell"
    missing = s.missing_indexed(need_fin, manual_margin_eur_mwh is not None)
    if missing:
        raise LookupError("paramètres à renseigner avant de pricer : " + ", ".join(missing))

    V = p.abs_volume
    direction = 1.0 if side == "sell" else -1.0
    idx = monthly_index(mkt, p, mkt.price)
    expected_index = float((idx["index €/MWh (ceil to cent)"] * idx["MWh"]).sum() / V)

    fees = s.epex_fee * V
    conv = s.conversion_fee * V
    if need_fin:
        d_estr, v_estr = rates.estr(mkt.pricing_date)
        rate = v_estr + s.spread_bp / 100
        fin = financing_cost(mkt, p, s, rate)
        fin_eur = float(fin["financing cost €"].sum())
        rate_ctx = {"€STR date": str(d_estr), "€STR %": v_estr, "spread bp": s.spread_bp, "funding rate %": rate}
    else:
        fin, fin_eur, rate_ctx = pd.DataFrame(), 0.0, {}
    rounding = ROUNDING_MAX * V if side == "buy" else 0.0
    years = _years(mkt, p)
    if manual_margin_eur_mwh is not None:
        mg = Margin(manual_margin_eur_mwh * V, "manual", {"€/MWh": manual_margin_eur_mwh, "reason": manual_reason})
    else:
        mg = margin_indexed(s, V, years)

    exact = (fees + conv + fin_eur + rounding + mg.eur) / V
    spread = ceil_cent(exact)
    sign = "+" if direction > 0 else "−"
    rows = [("Day-ahead fees", fees), ("Financial-to-physical conversion", conv), ("Funding cost", fin_eur),
            ("Index rounding to the cent (paid to the producer)", rounding), (f"Margin ({mg.rule})", mg.eur),
            ("= Spread (exact)", exact * V), ("Spread rounded up to the cent", (spread - exact) * V),
            ("= Spread quoted", spread * V)]
    lines = pd.DataFrame(rows, columns=["item", "€"]).set_index("item")
    lines["€/MWh"] = lines["€"] / V

    h = full_da_hedge(mkt, p)
    hv = hedge_summary(mkt, p, h)
    risk = RiskResult(0.0, 0.0, s.confidence, s.correlation,
                      pd.DataFrame(columns=["σ €", "mean €", "samples"]), pd.DataFrame())
    price = expected_index + direction * spread
    by_month = idx.assign(**{"spread €/MWh": spread,
                             f"expected invoice price €/MWh (index {sign} spread)":
                                 idx["index €/MWh (ceil to cent)"] + direction * spread})
    terms = {"formula": f"EPEX spot day-ahead {sign} {spread:.2f} €/MWh",
             "spread €/MWh": spread, "spread exact €/MWh": exact,
             "index": "monthly average of the EPEX day-ahead price over the delivered hours, weighted by the MWh "
                      "of each hour, rounded up to the cent",
             "by_month": by_month}
    warnings = []
    if need_fin:
        warnings.append("funding cost computed on the energy value at the HPFC: it moves with the realised spot")
    context = {"market": mkt.name, "pricing date": str(mkt.pricing_date), "pricing": INDEX,
               "strategy requested": INDEX, "strategy selected": f"{INDEX} (no hedge)",
               "formula": terms["formula"], "day-ahead fee €/MWh": s.epex_fee,
               "fin-to-phys conversion €/MWh": s.conversion_fee, "sourcing paid D+": s.sourcing_days,
               "client pays D+": s.client_days, "day count": s.day_count, **rate_ctx,
               "pricer config": s.hash_pricer, "margin config": s.hash_margin}
    return Quote(p, side, V, expected_index, price, lines, h, hv, risk, fin, mg, pd.DataFrame(), context, warnings,
                 None, None, terms)
