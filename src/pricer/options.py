"""Options européennes EEX sur futures Base France (mois, trimestre, année) : Black-76, greeks, volatilité.

Volatilité recalculée à chaque pricing, à partir de l'archive de power-hpfc (aucune valeur supposée) :
  - implicite : inversée des settlements d'options EEX de la séance de pricing (option hors de la monnaie :
    put sous le future, call au-dessus), interpolée linéairement entre strikes ; hors de la grille, prolongée à
    plat depuis le strike le plus proche (signalé) ;
  - historique (comparaison) : variations quotidiennes des settlements du même future archivés jusqu'à la séance,
    σ² = Σ r² / Σ Δt (Δt en années calendaires, cohérent avec l'échéance en ACT/365), incertitude ≈ σ / √(2n) ;
    non calculée sous `min_returns` variations (config).
Contrôles de qualité des settlements à chaque pricing : parité call − put = F − K, future implicite = settlement du
future, prix des calls décroissants et convexes, settlements inchangés depuis la séance précédente.

Conventions EEX (spécifications de contrat, à revérifier sur la version en vigueur) : options européennes, prime
non actualisée (constaté : parité exacte sans actualisation) ; dernier jour de négociation : option mois et
trimestre, 4 jours de bourse avant le début de livraison, sauf janvier et Q1 : 3e jeudi de décembre ; option année
(Cal) : 2e jeudi de décembre. Jours de bourse : lundi-vendredi hors 1er janvier, Vendredi saint, lundi de Pâques,
1er mai, 24, 25, 26 et 31 décembre (calendrier EEX Derivatives, applicable jusqu'à nouvel ordre).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import NormalDist

import numpy as np
import pandas as pd

_N = NormalDist()
TOL_PRICE = 0.002                   # €/MWh : settlements publiés au millième
TOL_FORWARD = 0.011                 # €/MWh : future implicite face au settlement du future (centime)


# ------------------------------------------------------------------ calendrier EEX
def easter(y: int) -> date:
    """Dimanche de Pâques (calendrier grégorien, algorithme de Meeus / Jones / Butcher)."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    wd = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * wd) // 451
    month = (h + wd - 7 * m + 114) // 31
    return date(y, month, (h + wd - 7 * m + 114) % 31 + 1)


def eex_holidays(y: int) -> set[date]:
    e = easter(y)
    return {date(y, 1, 1), e - timedelta(days=2), e + timedelta(days=1), date(y, 5, 1),
            date(y, 12, 24), date(y, 12, 25), date(y, 12, 26), date(y, 12, 31)}


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in eex_holidays(d.year)


def nth_weekday(y: int, m: int, weekday: int, n: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def last_trading_day(kind: str, start: date) -> date:
    """Dernier jour de négociation (= échéance) d'une option EEX sur le future Base livré à partir de `start`."""
    if kind == "year":
        return nth_weekday(start.year - 1, 12, 3, 2)              # 2e jeudi de décembre
    if kind in ("month", "quarter"):
        if start.month == 1:
            return nth_weekday(start.year - 1, 12, 3, 3)          # 3e jeudi de décembre
        d, n = start, 0
        while n < 4:
            d -= timedelta(days=1)
            n += is_trading_day(d)
        return d
    raise ValueError(f"pas d'option EEX sur un produit {kind!r}")


# ------------------------------------------------------------------ Black-76 (prime non actualisée)
def black76(F: float, K: float, T: float, sigma: float, call: bool) -> float:
    if T <= 0 or sigma <= 0:
        return max(F - K, 0.0) if call else max(K - F, 0.0)
    s = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * s * s) / s
    d2 = d1 - s
    return F * _N.cdf(d1) - K * _N.cdf(d2) if call else K * _N.cdf(-d2) - F * _N.cdf(-d1)


def greeks(F: float, K: float, T: float, sigma: float, call: bool) -> dict:
    """Delta, gamma (par €/MWh), vega (par point de volatilité), theta (par jour calendaire), pour 1 MWh."""
    s = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * s * s) / s
    pdf = _N.pdf(d1)
    return {"delta": _N.cdf(d1) if call else _N.cdf(d1) - 1.0,
            "gamma": pdf / (F * s),
            "vega": F * pdf * math.sqrt(T) / 100,
            "theta": -F * pdf * sigma / (2 * math.sqrt(T)) / 365}


def implied_vol(price: float, F: float, K: float, T: float, call: bool) -> float | None:
    """Volatilité implicite (bissection) ; None si le prix est hors des bornes de Black-76."""
    intrinsic = max(F - K, 0.0) if call else max(K - F, 0.0)
    upper = F if call else K
    if not (intrinsic + 1e-9 < price < upper - 1e-9) or T <= 0:
        return None
    lo, hi = 1e-6, 10.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if black76(F, K, T, mid, call) > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


# ------------------------------------------------------------------ nappe d'une série à une séance
@dataclass(frozen=True)
class Smile:
    name: str
    kind: str
    start: date
    end: date
    session: date
    expiry: date
    T: float
    forward: float                  # future implicite (parité)
    settlement: float               # settlement du future à la séance
    table: pd.DataFrame             # strike, call, put, vol
    checks: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def vol_at(self, K: float) -> tuple[float, str]:
        t = self.table.dropna(subset=["vol"])
        ks, vs = t["strike"].to_numpy(float), t["vol"].to_numpy(float)
        if K < ks[0]:
            return float(vs[0]), f"flat extrapolation below the lowest EEX strike ({ks[0]:g})"
        if K > ks[-1]:
            return float(vs[-1]), f"flat extrapolation above the highest EEX strike ({ks[-1]:g})"
        exact = np.isclose(ks, K)
        if exact.any():
            return float(vs[exact][0]), "EEX strike"
        return float(np.interp(K, ks, vs)), "interpolated between EEX strikes"


def build_smile(opts: pd.DataFrame, name: str, kind: str, start: date, end: date, session: date,
                settlement: float, previous: pd.DataFrame | None = None) -> Smile:
    """Nappe de la série `name` à la séance `session` (lignes de l'archive d'options de cette séance)."""
    expiry = last_trading_day(kind, start)
    if session >= expiry:
        raise ValueError(f"option {name} échue le {expiry} (séance {session})")
    g = opts[(opts["name"] == name) & (opts["session"] == session)].sort_values("strike")
    if g.empty:
        raise LookupError(f"aucun settlement d'option EEX {name} le {session} dans l'archive")
    T = (expiry - session).days / 365
    K, C, P = (g[c].to_numpy(float) for c in ("strike", "call", "put"))
    fwd = float(np.median(C - P + K))
    parity = float(np.max(np.abs(C - P - (fwd - K))))
    dC = np.diff(C)
    monotone = bool((dC <= TOL_PRICE).all())
    convex = bool((np.diff(C, 2) >= -TOL_PRICE).all()) if len(C) > 2 else True
    vols = [implied_vol(c, fwd, k, T, True) if k >= fwd else implied_vol(p, fwd, k, T, False)
            for k, c, p in zip(K, C, P)]
    table = pd.DataFrame({"strike": K, "call": C, "put": P, "vol": [np.nan if v is None else v for v in vols]})
    checks = {"strikes": int(len(K)), "strike range": f"{K.min():g} → {K.max():g}",
              "put-call parity max residual €/MWh": parity,
              "implied forward €/MWh": fwd, "future settlement €/MWh": settlement,
              "forward − settlement €/MWh": fwd - settlement,
              "calls decreasing in strike": monotone, "calls convex in strike": convex,
              "traded volume": float(np.nansum(g[["call_volume", "put_volume"]].to_numpy(float)))
              if {"call_volume", "put_volume"} <= set(g.columns) else None,
              "open interest": float(np.nansum(g[["call_open_interest", "put_open_interest"]].to_numpy(float)))
              if {"call_open_interest", "put_open_interest"} <= set(g.columns) else None}
    warnings = []
    if parity > TOL_PRICE:
        warnings.append(f"EEX option settlements {name}: put-call parity broken (residual {parity:.3f} €/MWh)")
    if abs(fwd - settlement) > TOL_FORWARD:
        warnings.append(f"EEX option settlements {name}: implied forward {fwd:.2f} ≠ future settlement "
                        f"{settlement:.2f} €/MWh")
    if not (monotone and convex):
        warnings.append(f"EEX option settlements {name}: call prices not decreasing / convex in strike (arbitrage)")
    if table["vol"].isna().any():
        warnings.append(f"EEX option settlements {name}: no implied volatility at "
                        f"{int(table['vol'].isna().sum())} strike(s) (price outside Black-76 bounds)")
    if not checks["traded volume"] and not checks["open interest"]:
        warnings.append(f"EEX option settlements {name}: no traded volume nor open interest — EEX valuations, "
                        "not traded prices")
    if previous is not None and len(previous):
        pv = previous.sort_values("strike")
        common = np.intersect1d(K, pv["strike"].to_numpy(float))
        if len(common):
            a = g.set_index("strike").loc[common, ["call", "put"]].to_numpy(float)
            b = pv.set_index("strike").loc[common, ["call", "put"]].to_numpy(float)
            unchanged = bool(np.allclose(a, b, atol=1e-9))
            checks["unchanged vs previous session"] = unchanged
            checks["previous session"] = str(pv["session"].iloc[0])
            if unchanged:
                warnings.append(f"EEX option settlements {name}: identical to the previous session "
                                f"({pv['session'].iloc[0]}) — possibly not updated")
    if table["vol"].notna().sum() == 0:
        raise ValueError(f"aucune volatilité implicite calculable pour {name} le {session}")
    return Smile(name, kind, start, end, session, expiry, T, fwd, settlement, table, checks, warnings)


# ------------------------------------------------------------------ volatilité historique du future
def historical_vol(history: pd.DataFrame, name: str, session: date, min_returns: int) -> dict:
    """Volatilité historique du future Base `name`, sur ses settlements archivés jusqu'à `session`."""
    h = history[(history["name"] == name) & (history["profile"] == "Base") & (history["session"] <= session)]
    h = h.dropna(subset=["settlement"]).drop_duplicates("session").sort_values("session")
    px = h["settlement"].to_numpy(float)
    days = list(h["session"])
    out = {"returns": max(len(px) - 1, 0), "first session": str(days[0]) if days else None,
           "last session": str(days[-1]) if days else None, "minimum returns": min_returns}
    if len(px) < 2 or (px <= 0).any():
        return {**out, "sigma": None, "reason": "fewer than 2 positive settlements archived"}
    r = np.diff(np.log(px))
    dt = np.array([(b - a).days for a, b in zip(days[:-1], days[1:])], float) / 365
    sigma = float(math.sqrt((r ** 2).sum() / dt.sum()))
    out.update({"sigma estimate": sigma, "standard error": sigma / math.sqrt(2 * len(r))})
    if len(r) < min_returns:
        return {**out, "sigma": None,
                "reason": f"{len(r)} daily returns archived, minimum {min_returns} for a usable estimate"}
    return {**out, "sigma": sigma, "reason": None}
