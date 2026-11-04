"""Couverture par produits EEX cotés et résidu à gérer en day-ahead.

Position du produit q, couverture équivalente h (même signe que q : on la traite en sens inverse sur EEX),
résidu r = q − h. r > 0 : surplus d'énergie à revendre en day-ahead ; r < 0 : manque à acheter en day-ahead.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from pricer.market import Market, month_end
from pricer.products import Product, require_on_grid

LEVELS = ("mois", "trimestre", "année")
# Granularité « monthly » : mois cotés, sinon trimestre, sinon année ; « quarterly » : trimestre, sinon année.
STRATEGIES = {
    "Base+Peak monthly": {"levels": ("mois", "trimestre", "année"), "peak": True},
    "Base+Peak quarterly": {"levels": ("trimestre", "année"), "peak": True},
    "Base only monthly": {"levels": ("mois", "trimestre", "année"), "peak": False},
    # Peak seul : les MWh peak de chaque produit coté sont couverts, tout l'offpeak livré reste en day-ahead
    # (risque de niveau porté par la prime de déviation)
    "Peak only + day-ahead": {"levels": ("mois", "trimestre", "année"), "peak": True, "peak_only": True},
}
KIND_LEVEL = {"month": "mois", "quarter": "trimestre", "year": "année"}


def quoted_blocks(mkt: Market, levels) -> dict:
    """Produits mois / trimestre / an cotés à la fois en Base et en Peak : {(début, fin): (nom, settl. Base, settl. Peak)}."""
    std = mkt.settle[mkt.settle["kind"].map(KIND_LEVEL).isin(levels)]
    both = std.pivot_table(index=["start", "end", "name"], columns="profile", values="settlement", aggfunc="first")
    if not {"Base", "Peak"} <= set(both.columns):
        return {}
    both = both.dropna(subset=["Base", "Peak"]).reset_index()
    return {(r.start, r.end): (r.name, r.Base, r.Peak) for r in both.itertuples()}


def partition(mkt: Market, levels=LEVELS) -> tuple[list, dict]:
    """Découpage le plus fin de la courbe en produits cotés (niveaux autorisés seulement)."""
    quoted = quoted_blocks(mkt, levels)

    def resolve(start, end, children):
        kids = [resolve(*c) for c in children]
        if kids and all(full for _, full in kids):
            return [x for pieces, _ in kids for x in pieces], True
        if (start, end) in quoted:
            return [(start, end)], True
        return [x for pieces, _ in kids for x in pieces], False

    pieces = []
    for y in range(mkt.first_day.year, mkt.last_day.year + 1):
        quarters = []
        for qn in range(4):
            months = [(date(y, m, 1), month_end(y, m), []) for m in range(3 * qn + 1, 3 * qn + 4)
                      if month_end(y, m) >= mkt.first_day and date(y, m, 1) <= mkt.last_day]
            if months:
                quarters.append((date(y, 3 * qn + 1, 1), month_end(y, 3 * qn + 3), months))
        pieces += resolve(date(y, 1, 1), date(y, 12, 31), quarters)[0]
    return sorted(pieces), quoted


@dataclass(frozen=True)
class Hedge:
    strategy: str
    table: pd.DataFrame          # une ligne par produit EEX utilisé
    hedged: pd.Series            # MW équivalents couverts, heure par heure (grille complète)
    residual: pd.Series          # q − h, heure par heure (grille complète)
    covered: pd.Series           # heures appartenant à un produit coté du découpage

    @property
    def surplus_mwh(self) -> float:
        return float(self.residual.clip(lower=0).sum())

    @property
    def deficit_mwh(self) -> float:
        return float(-self.residual.clip(upper=0).sum())


def hedge(mkt: Market, p: Product, strategy: str = "Base+Peak monthly") -> Hedge:
    """Couverture du produit par blocs EEX ; avec Peak, reproduit exactement les MWh Peak et Offpeak de chaque produit."""
    if strategy not in STRATEGIES:
        raise ValueError(f"stratégie inconnue {strategy!r} : {', '.join(STRATEGIES)}")
    require_on_grid(mkt, p)
    spec = STRATEGIES[strategy]
    pieces, quoted = partition(mkt, spec["levels"])
    q = p.mw.reindex(mkt.curve.index, fill_value=0.0)
    hedged = pd.Series(0.0, index=mkt.curve.index)
    covered = pd.Series(False, index=mkt.curve.index)
    rows = []
    for s, e in pieces:
        m = (mkt.day >= s) & (mkt.day <= e)
        covered |= m
        pk, op = m & mkt.is_peak, m & ~mkt.is_peak
        v_pk, v_op = float(q[pk].sum()), float(q[op].sum())
        if v_pk == 0 and v_op == 0:
            continue
        if spec.get("peak_only"):
            base, peak = 0.0, v_pk / pk.sum()
            if peak == 0:
                continue
        elif spec["peak"]:
            base = v_op / op.sum()
            peak = v_pk / pk.sum() - base
        else:
            base, peak = (v_pk + v_op) / m.sum(), 0.0
        hedged[m] += base
        hedged[pk] += peak
        name, s_base, s_peak = quoted[(s, e)]
        rows.append({"EEX contract": name, "start": s, "end": e, "Base MW": base, "Peak MW": peak,
                     "Base settl.": s_base, "Peak settl.": s_peak,
                     "Base MWh": base * m.sum(), "Peak MWh": peak * pk.sum(),
                     "hedge value €": base * m.sum() * s_base + peak * pk.sum() * s_peak})
    table = pd.DataFrame(rows).set_index("EEX contract") if rows else pd.DataFrame()
    return Hedge(strategy, table, hedged, q - hedged, covered)


def hedge_summary(mkt: Market, p: Product, h: Hedge) -> dict:
    """Valeur attendue (EUR, signée, du point de vue de la position) : couverture aux settlements + résidu sur la HPFC."""
    px = mkt.price
    hedge_value = float(h.table["hedge value €"].sum()) if len(h.table) else 0.0
    resid_value = float((h.residual * px).sum())
    curve_value = float((p.mw * px[p.mw.index]).sum())
    return {"curve value €": curve_value, "EEX hedge at settlement €": hedge_value,
            "residual at HPFC €": resid_value,
            "settlement rounding €": curve_value - hedge_value - resid_value,
            "EEX hedged MWh": float(h.hedged.abs().sum()),
            "EEX contracts": int(len(h.table)),
            "day-ahead surplus to sell MWh": h.surplus_mwh,
            "day-ahead shortfall to buy MWh": h.deficit_mwh,
            "unhedgeable MWh (no quoted month/quarter/year)": float(p.mw[~h.covered[p.mw.index]].abs().sum())}


def level_open(mkt: Market, p: Product, h: Hedge) -> pd.Series:
    """MW du produit dont le niveau de prix n'est couvert par aucun contrat EEX : heures sans produit coté
    (mois en cours) et, pour « Peak only », toutes les heures offpeak livrées."""
    mask = ~h.covered[p.mw.index]
    if STRATEGIES.get(h.strategy, {}).get("peak_only"):
        mask |= ~mkt.is_peak[p.mw.index]
    return p.mw[mask]


def coverage(mkt: Market, p: Product, h: Hedge) -> pd.DataFrame:
    """Couverture heure par heure, en MW positifs dans le sens du deal :
    delivered (livré au client ou reçu de la contrepartie), EEX Base, EEX Peak (MW traités sur EEX en sens
    inverse du deal ; négatif = même sens), day-ahead purchase et day-ahead resale (résidu r = q − h : r < 0 acheté,
    r > 0 revendu). Heures sans livraison ni couverture omises."""
    sgn = -1.0 if p.side == "sell" else 1.0
    idx = mkt.curve.index
    q = p.mw.reindex(idx, fill_value=0.0)
    base = pd.Series(0.0, index=idx)
    peak = pd.Series(0.0, index=idx)
    t = h.table
    if len(t) and "Base MW" in t.columns:
        for r in t.itertuples():
            m = (mkt.day >= r.start) & (mkt.day <= r.end)
            base[m] += t.loc[r.Index, "Base MW"]
            peak[m & mkt.is_peak] += t.loc[r.Index, "Peak MW"]
    elif len(t) and "MW" in t.columns:                     # contrat coté couvert par lui-même
        name = str(t.index[0])
        target = peak if name.startswith("Peak") else base
        target[h.hedged != 0] = h.hedged[h.hedged != 0]
    resid = q - (base + peak)
    out = pd.DataFrame({"delivered MW": sgn * q, "EEX Base MW": sgn * base, "EEX Peak MW": sgn * peak,
                        "day-ahead purchase MW": (-resid).clip(lower=0), "day-ahead resale MW": resid.clip(lower=0)})
    out = out[(out.abs() > 1e-9).any(axis=1)]
    return out


def typical_week(mkt: Market, cov: pd.DataFrame, column: str, month: str | None = None) -> pd.DataFrame:
    """Semaine type (24 heures × 7 jours) : moyenne de `column` sur chaque (jour de semaine, heure locale), sur la
    période couverte par `cov` ou sur un mois (AAAA-MM). Les jours sans aucune ligne comptent pour 0."""
    days = mkt.day[cov.index]
    first, last = days.min(), days.max()
    span = (mkt.day >= first) & (mkt.day <= last)
    if month is not None:
        span &= mkt.month == month
    idx = mkt.curve.index[span.to_numpy()]
    s = cov[column].reindex(idx, fill_value=0.0)
    loc = idx.tz_convert(mkt.tz)
    g = pd.DataFrame({"v": s.to_numpy(), "h": loc.hour, "d": loc.weekday}).groupby(["h", "d"])["v"].mean()
    out = g.unstack("d").reindex(index=range(24), columns=range(7)).fillna(0.0)
    out.index = [f"{h:02d}h-{h + 1:02d}h" for h in range(24)]
    out.columns = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return out


LISTED = "Listed EEX contract"


def listed_contract(mkt: Market, p: Product):
    """Le contrat EEX coté dont le produit est la copie exacte (mêmes heures, MW constants), sinon None."""
    if p.mixed or p.mw.nunique() != 1:
        return None
    for r in mkt.settle.itertuples():
        m = (mkt.day >= r.start) & (mkt.day <= r.end)
        if r.profile == "Peak":
            m &= mkt.is_peak
        idx = mkt.curve.index[m.to_numpy()]
        if len(idx) == len(p.mw) and idx.equals(p.mw.index):
            return r
    return None


def listed_hedge(mkt: Market, p: Product, r) -> Hedge:
    """Un contrat coté se couvre par lui-même : pas de résidu, rien en day-ahead."""
    mw, n = float(p.mw.iloc[0]), len(p.mw)
    table = pd.DataFrame([{"EEX contract": f"{r.profile} {r.name}", "start": r.start, "end": r.end,
                           "MW": mw, "settlement": r.settlement, "MWh": mw * n,
                           "hedge value €": mw * n * r.settlement}]).set_index("EEX contract")
    hedged = p.mw.reindex(mkt.curve.index, fill_value=0.0)
    return Hedge(LISTED, table, hedged, hedged * 0.0, pd.Series(True, index=mkt.curve.index))


FULL_DA = "Full day-ahead"


def full_da_hedge(mkt: Market, p: Product) -> Hedge:
    """Aucune couverture EEX : tout le volume est acheté (ou revendu) en day-ahead. `covered` est vrai partout :
    le risque de niveau de tout le volume est porté par la prime de déviation, pas par l'avertissement
    « sans couverture possible »."""
    require_on_grid(mkt, p)
    q = p.mw.reindex(mkt.curve.index, fill_value=0.0)
    return Hedge(FULL_DA, pd.DataFrame(), q * 0.0, q, pd.Series(True, index=mkt.curve.index))
