"""Valorisation sur la HPFC : prix juste, valeur face à un prix K, décomposition par période et par bloc."""
from __future__ import annotations

import numpy as np
import pandas as pd

from pricer.market import Market
from pricer.products import Product, require_on_grid


def fair_price(mkt: Market, p: Product) -> float:
    """Moyenne de la courbe pondérée par les volumes (EUR/MWh). Non défini si achats et ventes sont mélangés."""
    require_on_grid(mkt, p)
    if p.mixed:
        raise ValueError(f"{p.name} : achats et ventes mélangés, le prix par MWh n'a pas de sens")
    q = p.mw
    return float((q * mkt.price[q.index]).sum() / q.sum())


def _k_series(p: Product, K) -> pd.Series:
    if np.isscalar(K):
        return pd.Series(float(K), index=p.mw.index)
    k = K.tz_convert("UTC").reindex(p.mw.index)
    if k.isna().any():
        raise ValueError(f"{p.name} : prix K manquant sur {int(k.isna().sum())} heures")
    return k


def value(mkt: Market, p: Product, K) -> float:
    """Valeur pour nous (EUR, non actualisée) : Σ q · (P − K). K constant ou horaire."""
    require_on_grid(mkt, p)
    q = p.mw
    return float((q * (mkt.price[q.index] - _k_series(p, K))).sum())


def decompose(mkt: Market, p: Product, K, by: str = "mois") -> pd.DataFrame:
    """Valeur par période (mois, trimestre, année) et par bloc Peak / Offpeak, avec une ligne Total."""
    require_on_grid(mkt, p)
    q = p.mw
    k = _k_series(p, K)
    mois = mkt.month[q.index]
    year, month = mois.str[:4], mois.str[5:].astype(int)
    periods = {"mois": mois, "trimestre": year + "-T" + ((month - 1) // 3 + 1).astype(str), "année": year}
    if by not in periods:
        raise ValueError(f"découpage inconnu {by!r} : {list(periods)}")
    h = pd.DataFrame({"periode": periods[by], "bloc": np.where(mkt.is_peak[q.index], "Peak", "Offpeak"),
                      "mwh": q, "courbe": q * mkt.price[q.index], "contrat": q * k,
                      "pos": q > 0, "neg": q < 0})

    def table(g):
        t = g.agg(heures=("mwh", "size"), mwh=("mwh", "sum"), courbe=("courbe", "sum"),
                  contrat=("contrat", "sum"), pos=("pos", "any"), neg=("neg", "any"))
        return pd.DataFrame({"heures": t["heures"], "volume MWh": t["mwh"], "MW moyen": t["mwh"] / t["heures"],
                             "prix courbe": (t["courbe"] / t["mwh"]).where(~(t["pos"] & t["neg"])),
                             "valeur €": t["courbe"] - t["contrat"]})

    out = table(h.groupby(["periode", "bloc"]))
    total = table(h.assign(periode="Total", bloc="").groupby(["periode", "bloc"]))
    return pd.concat([out, total])
