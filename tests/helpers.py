"""Marché synthétique déterministe pour les tests (aucune donnée réelle, aucun accès réseau)."""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd

from pricer.market import Market, month_end
from pricer.settings import Settings

TZ = "Europe/Paris"
PRICING = date(2026, 10, 13)


def hours(start: date, end: date) -> pd.DatetimeIndex:
    loc = pd.date_range(pd.Timestamp(start, tz=TZ), pd.Timestamp(end + timedelta(days=1), tz=TZ),
                        freq="h", inclusive="left")
    return loc.tz_convert("UTC")


def peak(index: pd.DatetimeIndex) -> np.ndarray:
    loc = index.tz_convert(TZ)
    return (loc.weekday < 5) & (loc.hour >= 8) & (loc.hour < 20)


def shape(index: pd.DatetimeIndex) -> np.ndarray:
    loc = index.tz_convert(TZ)
    h, wd = loc.hour.to_numpy(), loc.weekday.to_numpy()
    return 1 + 0.25 * np.sin((h - 6) / 24 * 2 * np.pi) + np.where(wd >= 5, -0.12, 0.03)


def curve() -> pd.DataFrame:
    idx = hours(PRICING + timedelta(days=1), date(2028, 12, 31))
    loc = idx.tz_convert(TZ)
    level = 90 + 30 * np.cos((loc.month.to_numpy() - 1) / 12 * 2 * np.pi) + 2 * (loc.year.to_numpy() - 2027)
    return pd.DataFrame({"price": level * shape(idx), "is_peak": peak(idx)}, index=idx)


def settlements(c: pd.DataFrame) -> pd.DataFrame:
    day = pd.Series(c.index.tz_convert(TZ).date, index=c.index)
    products = [("month", f"M{m:02d}-{y % 100}", date(y, m, 1), month_end(y, m))
                for y, m in [(2026, 11), (2026, 12), (2027, 1), (2027, 2), (2027, 3)]]
    products += [("quarter", f"Q{q}-27", date(2027, 3 * q - 2, 1), month_end(2027, 3 * q)) for q in (2, 3, 4)]
    products += [("quarter", "Q1-27", date(2027, 1, 1), date(2027, 3, 31)),
                 ("year", "Cal-27", date(2027, 1, 1), date(2027, 12, 31)),
                 ("year", "Cal-28", date(2028, 1, 1), date(2028, 12, 31))]
    rows = []
    for kind, name, s, e in products:
        m = (day >= s) & (day <= e)
        for prof in ("Base", "Peak"):
            mm = m & c["is_peak"] if prof == "Peak" else m
            rows.append({"profile": prof, "kind": kind, "name": name, "start": s, "end": e,
                         "settlement": round(float(c.loc[mm, "price"].mean()), 2)})
    return pd.DataFrame(rows)


def spot_history() -> pd.Series:
    idx = hours(date(2019, 1, 1), date(2026, 9, 30))
    loc = idx.tz_convert(TZ)
    rng = np.random.default_rng(7)
    level = 60 + 25 * np.cos((loc.month.to_numpy() - 1) / 12 * 2 * np.pi)
    noise = rng.normal(0, 0.08, len(idx))
    return pd.Series(level * shape(idx) * (1 + noise), index=idx)


@lru_cache(maxsize=1)
def market() -> Market:
    c = curve()
    spot = spot_history()
    return Market(name="FR", pricing_date=PRICING, tz=TZ, unit="EUR/MWh", tol=0.01, peak_hours=(8, 20),
                  peak_weekdays=(0, 1, 2, 3, 4), curve=c, settle=settlements(c),
                  meta={"method": "test", "code_version": "test"},
                  spot_loader=lambda s, e: spot[(spot.index.tz_convert(TZ).date >= s)
                                                & (spot.index.tz_convert(TZ).date <= e)])


def settings(**over) -> Settings:
    base = dict(sourcing_days=5, client_days=50, day_count=360, spread_bp=150.0, eex_fee=0.02, epex_fee=0.05, conversion_fee=0.095,
                liquidity_threshold_mw=5.0, liquidity_eur_mwh_per_mw=0.01,
                confidence=0.95, window=1, correlation=0.0, history_start=date(2019, 1, 1),
                strategy="auto", margin_base=1.0, margin_shape_pct=10.0, margin_per_year=0.5,
                margin_floor=500.0, deviation_lookback=30, deviation_day_window=7, deviation_min_months=12,
                deviation_excluded=((date(2021, 9, 1), date(2023, 6, 30)),), full_day_ahead=True,
                margin_index_base=0.30, margin_index_per_year=0.05, margin_index_floor=100.0,
                option_fee=None, option_vol_premium_pts=None, option_min_returns=None,
                margin_option_base=None, margin_option_floor=None)
    base.update(over)
    return Settings(**base)
