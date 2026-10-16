"""TBx (écart top x heures − bottom x heures d'un jour), batterie en valeur intrinsèque, backtest HPFC / spot.

Le TBx est convexe en prix : celui d'une courbe de prix attendus est un plancher du TBx attendu.
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from pricer.market import Market
from pricer.products import Product

TB_HOURS = (1, 2, 4)


def tb_select(prices: pd.Series, tz: str, x: int):
    day = pd.Series(prices.index.tz_convert(tz).date, index=prices.index)
    n = day.groupby(day).transform("size")
    if (n < 23).any() or (n > 25).any():
        raise ValueError("journées incomplètes dans la série de prix : TBx non calculable")
    if 2 * x > 23:
        raise ValueError(f"TB{x} impossible : 2 × {x} heures dépassent une journée")
    g = prices.groupby(day)
    top = g.rank(method="first", ascending=False) <= x
    bottom = g.rank(method="first", ascending=True) <= x
    return top, bottom, day


def tbx_daily(prices: pd.Series, tz: str, x: int) -> pd.DataFrame:
    top, bottom, day = tb_select(prices, tz, x)
    d = pd.DataFrame({"top": prices[top].groupby(day[top]).mean(),
                      "bottom": prices[bottom].groupby(day[bottom]).mean()})
    d[f"TB{x}"] = d["top"] - d["bottom"]
    return d


def tbx_mean(prices: pd.Series, tz: str) -> pd.Series:
    return pd.Series({f"TB{x}": tbx_daily(prices, tz, x)[f"TB{x}"].mean() for x in TB_HOURS})


def battery(mkt: Market, start: date, end: date, mw: float, x: int, eta: float) -> tuple[Product, pd.DataFrame]:
    """Charge mw pendant les x heures les moins chères, restitue eta × mw pendant les x plus chères, chaque jour
    où eta × top > bottom. Sans contrainte de chronologie ni d'état de charge : valeur intrinsèque (plancher)."""
    if not 0 < eta <= 1:
        raise ValueError("rendement aller-retour attendu dans ]0, 1]")
    px = mkt.price[mkt.days(start, end)]
    top, bottom, day = tb_select(px, mkt.tz, x)
    d = tbx_daily(px, mkt.tz, x)
    d["actif"] = eta * d["top"] > d["bottom"]
    on = day.map(d["actif"]).astype(bool)
    q = pd.Series(0.0, index=px.index)
    q[bottom & on] = mw
    q[top & on] = -eta * mw
    d["marge €"] = np.where(d["actif"], mw * x * (eta * d["top"] - d["bottom"]), 0.0)
    return Product(f"Batterie {mw:g} MW / {x} h, rendement {eta:.0%}", q[q != 0], physical=False), d


def backtest(mkt: Market) -> pd.DataFrame:
    """Pour chaque courbe archivée du jour J : TBx de la HPFC sur J+1 contre TBx du spot réalisé de J+1."""
    days = [d for d in mkt.available_curves() if d + timedelta(days=1) <= mkt.pricing_date]
    if not days:
        raise LookupError("aucune courbe archivée dont le lendemain est déjà réalisé")
    spot = mkt.spot(days[0] + timedelta(days=1), days[-1] + timedelta(days=1))
    spot_day = spot.index.tz_convert(mkt.tz).date
    rows = []
    for d in days:
        nxt = d + timedelta(days=1)
        c = mkt.load_other_curve(d)
        hp = c[c.index.tz_convert(mkt.tz).date == nxt]
        sp = spot[spot_day == nxt]
        if len(hp) != len(sp) or len(hp) < 23:
            raise ValueError(f"heures incohérentes le {nxt} : HPFC {len(hp)}, spot {len(sp)}")
        row = {"livraison": nxt, "HPFC base": hp.mean(), "spot base": sp.mean()}
        for x in TB_HOURS:
            row[f"HPFC TB{x}"] = tbx_daily(hp, mkt.tz, x)[f"TB{x}"].item()
            row[f"spot TB{x}"] = tbx_daily(sp, mkt.tz, x)[f"TB{x}"].item()
        rows.append(row)
    return pd.DataFrame(rows).set_index("livraison")
