"""Prime de déviation day-ahead : risque de niveau d'un volume sourcé (ou revendu) en day-ahead sans couverture EEX.

Méthode actuelle : approximation prudente par PERSISTANCE, mesurée sur l'historique spot day-ahead ENTSO-E.
Le forward historique gratuit n'existe pas (l'API EEX ne garde qu'environ 6 semaines de séances) : on mesure à la
place l'erreur d'une prévision naïve, qui est plus grossière qu'une HPFC et surestime donc l'erreur réelle.

Pour chaque jour d'origine t de l'historique (jour du mois à ± `day_window` jours de la date de pricing) :
  prévision du bloc b (Peak, Offpeak) pour le mois cible = moyenne spot du bloc sur les `lookback` derniers jours
      × saisonnalité du mois cible ÷ saisonnalité moyenne de la fenêtre ;
  réalisé = moyenne spot du bloc sur le mois cible (k ≥ 1 mois après le mois de t) ou sur le reste du mois de t
      (k = 0) ;
  erreur relative e = réalisé ÷ prévision − 1.
La saisonnalité d'un mois calendaire est la moyenne, sur les années civiles complètes hors périodes exclues, du
rapport moyenne du mois ÷ moyenne de l'année (estimée sur tout l'historique : léger biais de connaissance du
futur, assumé pour un proxy).
Écart d'un horizon k = RMSE de e (biais compris, donc prudent), si au moins `min_months` mois cibles distincts.

Deal : pour chaque mois livré, σ_mois = |valeur Peak à la HPFC| × RMSE_Peak(k) + |valeur Offpeak| × RMSE_Offpeak(k)
(somme, sans diversification entre blocs) ; σ = somme des σ_mois (mois parfaitement corrélés) ; prime = z × σ.
Les périodes exclues (crise 2021-2023 par défaut) ne servent ni d'origine, ni de cible, ni à la saisonnalité.

À remplacer, horizon par horizon, par la mesure exacte (HPFC archivées face au day-ahead réalisé, calculée chaque
jour par power-hpfc dans data/accuracy/) quand l'archive des courbes sera assez longue.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import NormalDist

import numpy as np
import pandas as pd

from pricer.market import Market

METHOD = "persistence proxy (conservative)"
BLOCKS = ("Peak", "Offpeak")


@dataclass(frozen=True)
class DeviationModel:
    errors: pd.DataFrame             # origin, k, block, target month, error (relative)
    seasonal: pd.DataFrame           # mois calendaire × bloc
    lookback: int
    day_window: int
    min_months: int
    excluded: tuple
    history: tuple                   # (premier jour, dernier jour) du spot utilisé

    def rmse(self, k: int, block: str) -> tuple[float | None, int, int]:
        """RMSE de l'erreur relative à l'horizon k (mois), nombre d'observations, mois cibles distincts."""
        e = self.errors[(self.errors["k"] == k) & (self.errors["block"] == block)]
        n_m = int(e["target month"].nunique())
        if n_m < self.min_months:
            return None, len(e), n_m
        return float(np.sqrt((e["error"] ** 2).mean())), len(e), n_m

    def table(self, max_k: int) -> pd.DataFrame:
        rows = []
        for k in range(max_k + 1):
            row = {"horizon (months)": k}
            for b in BLOCKS:
                r, n, n_m = self.rmse(k, b)
                row.update({f"{b} RMSE %": None if r is None else 100 * r, f"{b} obs.": n, f"{b} months": n_m})
            rows.append(row)
        return pd.DataFrame(rows).set_index("horizon (months)")


@dataclass(frozen=True)
class DeviationResult:
    cost: float                      # EUR, ≥ 0
    sigma: float                     # EUR
    confidence: float
    by_month: pd.DataFrame           # par mois livré : horizon, valeurs Peak / Offpeak, RMSE, σ
    method: str = METHOD


def _excluded_mask(days: pd.DatetimeIndex, excluded) -> np.ndarray:
    d = days.date
    m = np.zeros(len(d), bool)
    for a, b in excluded:
        m |= (d >= a) & (d <= b)
    return m


def build_model(mkt: Market, spot: pd.Series, lookback: int, excluded, day_window: int,
                min_months: int) -> DeviationModel:
    if lookback < 7:
        raise ValueError("deviation : fenetre_jours ≥ 7 attendu")
    loc = spot.index.tz_convert(mkt.tz)
    pk = mkt.peak_mask(spot.index)
    df = pd.DataFrame({"day": loc.normalize().tz_localize(None), "p": spot.to_numpy(float), "pk": pk})
    days = pd.date_range(df["day"].min(), df["day"].max(), freq="D")
    sums, cnts = {}, {}
    for b, sel in (("Peak", df["pk"]), ("Offpeak", ~df["pk"])):
        g = df[sel].groupby("day")["p"]
        sums[b] = g.sum().reindex(days, fill_value=0.0).to_numpy()
        cnts[b] = g.count().reindex(days, fill_value=0).to_numpy().astype(float)
    total = cnts["Peak"] + cnts["Offpeak"]
    if (total < 23).any():
        bad = days[total < 23][0].date()
        raise ValueError(f"spot historique incomplet le {bad} : prime de déviation impossible")
    excl = _excluded_mask(days, excluded)
    ym = days.year * 12 + days.month - 1                          # identifiant de mois
    cal = days.month.to_numpy()

    # moyennes mensuelles par bloc et mois exclus / incomplets
    months = np.unique(ym)
    m_mean = {b: {} for b in BLOCKS}
    m_ok = {}
    for m in months:
        sel = ym == m
        first, n = days[sel][0], sel.sum()
        full = first.day == 1 and n == first.days_in_month
        m_ok[m] = bool(full and not excl[sel].any())
        for b in BLOCKS:
            c = cnts[b][sel].sum()
            m_mean[b][m] = sums[b][sel].sum() / c if c else np.nan

    # saisonnalité : années civiles complètes hors exclusion
    years = [y for y in sorted(set(days.year)) if all(m_ok.get(y * 12 + i, False) for i in range(12))]
    if len(years) < 2:
        raise LookupError(f"saisonnalité : {len(years)} année(s) civile(s) complète(s) hors périodes exclues "
                          "(minimum 2)")
    seas = {}
    for b in BLOCKS:
        ratios = np.zeros((len(years), 12))
        for i, y in enumerate(years):
            sel = (days.year == y)
            annual = sums[b][sel].sum() / cnts[b][sel].sum()
            if annual <= 0:
                raise ValueError(f"prix moyen {b} {y} non positif : saisonnalité impossible")
            ratios[i] = [m_mean[b][y * 12 + j] / annual for j in range(12)]
        seas[b] = ratios.mean(axis=0)
    seasonal = pd.DataFrame(seas, index=pd.Index(range(1, 13), name="calendar month"))

    # sommes cumulées pour les fenêtres et les restes de mois
    def cum(a):
        return np.concatenate([[0.0], np.cumsum(a)])
    cs = {b: cum(sums[b]) for b in BLOCKS}
    cc = {b: cum(cnts[b]) for b in BLOCKS}
    csc = {b: cum(cnts[b] * seas[b][cal - 1]) for b in BLOCKS}     # pour la saisonnalité moyenne de la fenêtre
    cex = cum(excl.astype(float))
    month_end_idx = {}
    for m in months:
        month_end_idx[m] = int(np.flatnonzero(ym == m)[-1])

    target_dom = mkt.pricing_date.day
    dom = days.day.to_numpy()
    last_m = months.max()
    rows = []
    for i in range(lookback - 1, len(days)):
        if abs(int(dom[i]) - target_dom) > day_window:
            continue
        lo = i - lookback + 1
        if cex[i + 1] - cex[lo] > 0:
            continue
        m0 = int(ym[i])
        for b in BLOCKS:
            c = cc[b][i + 1] - cc[b][lo]
            if c == 0:
                continue
            level = (cs[b][i + 1] - cs[b][lo]) / c
            sbar = (csc[b][i + 1] - csc[b][lo]) / c
            # k = 0 : reste du mois de l'origine
            j = month_end_idx[m0]
            if j > i and cex[j + 1] - cex[i + 1] == 0:
                cr = cc[b][j + 1] - cc[b][i + 1]
                if cr > 0:
                    f = level * seas[b][m0 % 12] / sbar
                    if f > 0:
                        r = (cs[b][j + 1] - cs[b][i + 1]) / cr
                        rows.append((days[i].date(), 0, b, m0, r / f - 1))
            for k in range(1, int(last_m - m0) + 1):
                mt = m0 + k
                if not m_ok.get(mt, False) or not np.isfinite(m_mean[b][mt]):
                    continue
                f = level * seas[b][mt % 12] / sbar
                if f > 0:
                    rows.append((days[i].date(), k, b, mt, m_mean[b][mt] / f - 1))
    errors = pd.DataFrame(rows, columns=["origin", "k", "block", "target month", "error"])
    errors["target month"] = errors["target month"].map(lambda m: f"{m // 12}-{m % 12 + 1:02d}")
    return DeviationModel(errors, seasonal, lookback, day_window, min_months, tuple(excluded),
                          (days[0].date(), days[-1].date()))


def months_ahead(pricing: date, month: str) -> int:
    y, m = int(month[:4]), int(month[5:7])
    return (y - pricing.year) * 12 + m - pricing.month


def deviation_premium(mkt: Market, mw: pd.Series, model: DeviationModel, confidence: float) -> DeviationResult:
    """Prime de déviation des volumes `mw` (MW signés, heures de la courbe) traités en day-ahead sans couverture."""
    mw = mw[mw != 0]
    z = NormalDist().inv_cdf(confidence)
    if mw.empty:
        return DeviationResult(0.0, 0.0, confidence, pd.DataFrame())
    value = mw * mkt.price[mw.index]
    month = mkt.month[mw.index]
    peak = mkt.is_peak[mw.index]
    rows = []
    for mo in sorted(month.unique()):
        k = months_ahead(mkt.pricing_date, mo)
        row = {"month": mo, "horizon (months)": k}
        sig = 0.0
        for b, sel in (("Peak", peak), ("Offpeak", ~peak)):
            v = float(value[(month == mo) & sel].sum())
            r, n, n_m = model.rmse(k, b) if v != 0 else (0.0, 0, 0)
            if r is None:
                raise LookupError(f"prime de déviation : historique insuffisant à {k} mois pour le bloc {b} "
                                  f"({n_m} mois cibles, minimum {model.min_months})")
            row.update({f"{b} value €": v, f"{b} RMSE %": 100 * r, f"{b} obs.": n})
            sig += abs(v) * r
        row["σ €"] = sig
        rows.append(row)
    by_month = pd.DataFrame(rows).set_index("month")
    sigma = float(by_month["σ €"].sum())
    return DeviationResult(z * sigma, sigma, confidence, by_month)
