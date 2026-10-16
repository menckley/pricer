"""Coût de risque du résidu de couverture, mesuré sur l'historique spot day-ahead.

Pour chaque mois livré m, le résidu (produit − couverture EEX) est résumé par sa semaine type : MW moyen par
(jour de semaine, heure locale). Cette semaine type est rejouée sur chaque mois historique de la même saison
(même mois calendaire ± `window` mois), recentrée pour avoir un volume nul sur chaque bloc couvert
(Peak et Offpeak si la couverture a du Peak, Base sinon), puis valorisée au spot réalisé. Le résultat est
rapporté au prix moyen du mois historique et remis au niveau forward du mois m.

Hypothèses affichées : P&L mensuels de loi normale pour le quantile, corrélation uniforme ρ entre mois.
Non couvert ici : le risque de niveau d'un mois à l'intérieur d'un trimestre ou d'une année couverts en bloc.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import NormalDist

import numpy as np
import pandas as pd

from pricer.hedging import STRATEGIES, Hedge
from pricer.market import Market

MIN_SAMPLES = 5


@dataclass(frozen=True)
class RiskResult:
    cost: float                      # EUR, ≥ 0
    sigma: float                     # EUR, écart-type du P&L total du résidu
    confidence: float
    correlation: float
    by_month: pd.DataFrame           # σ, moyenne, nombre d'échantillons par mois livré
    samples: pd.DataFrame            # P&L remis à l'échelle, par mois livré et mois historique


def history_bounds(mkt: Market, start: date) -> tuple[date, date]:
    """Du début demandé au dernier jour du dernier mois complet avant la date de pricing."""
    end = date(mkt.pricing_date.year, mkt.pricing_date.month, 1) - timedelta(days=1)
    if start > end:
        raise ValueError("historique spot vide")
    return start, end


def _cell(index: pd.DatetimeIndex, tz: str) -> np.ndarray:
    loc = index.tz_convert(tz)
    return loc.weekday.to_numpy() * 24 + loc.hour.to_numpy()


def residual_risk(mkt: Market, h: Hedge, spot: pd.Series, confidence: float = 0.95, window: int = 1,
                  correlation: float = 0.0) -> RiskResult:
    if not 0.5 < confidence < 1:
        raise ValueError("niveau de confiance attendu dans ]0,5 ; 1[")
    if not 0 <= correlation <= 1:
        raise ValueError("corrélation attendue dans [0 ; 1]")
    # recentrage par bloc Peak / Offpeak (Base seul pour « Base only ») ; full day-ahead : par bloc, le niveau
    # de chaque bloc étant porté par la prime de déviation
    by_block = STRATEGIES[h.strategy]["peak"] if h.strategy in STRATEGIES else True

    s_loc = spot.index.tz_convert(mkt.tz)
    s = pd.DataFrame({"price": spot.to_numpy(float), "ym": s_loc.strftime("%Y-%m"), "m": s_loc.month,
                      "cell": _cell(spot.index, mkt.tz), "peak": mkt.peak_mask(spot.index)}, index=spot.index)
    hist = {ym: (g["cell"].to_numpy(), g["peak"].to_numpy(), g["price"].to_numpy())
            for ym, g in s.groupby("ym", sort=True)}

    resid = h.residual[h.residual != 0]
    if resid.empty:
        return RiskResult(0.0, 0.0, confidence, correlation,
                          pd.DataFrame(columns=["σ €", "mean €", "samples"]), pd.DataFrame())
    r_month = mkt.month[resid.index]
    rows, samples = [], []
    for mois in sorted(r_month.unique()):
        rm = resid[r_month == mois]
        g = np.zeros(168)
        cells = _cell(rm.index, mkt.tz)
        sums = np.bincount(cells, weights=rm.to_numpy(), minlength=168)
        counts = np.bincount(_cell(mkt.curve.index[mkt.month == mois], mkt.tz), minlength=168)
        np.divide(sums, counts, out=g, where=counts > 0)
        fwd_level = float(mkt.price[mkt.month == mois].mean())
        cal_m = int(mois[5:])
        season = {(cal_m - 1 + k) % 12 + 1 for k in range(-window, window + 1)}
        pnl = {}
        for ym, (cell, pk, price) in hist.items():
            if int(ym[5:]) not in season:
                continue
            r = g[cell]
            groups = [pk, ~pk] if by_block else [np.ones(len(cell), bool)]
            for blk in groups:
                if blk.any():
                    r[blk] -= r[blk].mean()
            level = float(price.mean())
            if level <= 0:
                raise ValueError(f"prix spot moyen non positif en {ym} : remise à l'échelle impossible")
            pnl[ym] = float((r * price).sum()) / level * fwd_level
        if len(pnl) < MIN_SAMPLES:
            raise LookupError(f"historique spot insuffisant pour {mois} : {len(pnl)} mois comparables "
                              f"(minimum {MIN_SAMPLES})")
        v = np.array(list(pnl.values()))
        rows.append({"month": mois, "σ €": float(v.std(ddof=1)), "mean €": float(v.mean()),
                     "samples": len(v), "worst €": float(v.min()), "best €": float(v.max())})
        samples += [{"month": mois, "historical month": k, "P&L €": x} for k, x in pnl.items()]

    by_month = pd.DataFrame(rows).set_index("month")
    sig = by_month["σ €"].to_numpy()
    var = float((sig ** 2).sum() + correlation * (sig.sum() ** 2 - (sig ** 2).sum()))
    sigma = float(np.sqrt(var))
    z = NormalDist().inv_cdf(confidence)
    return RiskResult(z * sigma, sigma, confidence, correlation, by_month, pd.DataFrame(samples))
