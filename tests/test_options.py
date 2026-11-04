"""Options EEX : calendrier, Black-76, nappe, volatilité historique, pricing des structures."""
import math
from dataclasses import replace
from datetime import UTC, date, timedelta

import numpy as np
import pandas as pd
import pytest

from pricer.option_quote import option_series, quote_option
from pricer.options import (
    black76,
    build_smile,
    easter,
    greeks,
    historical_vol,
    implied_vol,
    is_trading_day,
    last_trading_day,
)
from tests.helpers import PRICING, market, settings

SIGMA = 0.30                     # volatilité des settlements d'options synthétiques
HIST = 0.40                      # volatilité des settlements de futures synthétiques


def _options_frame(m, sigma=SIGMA, sessions=(PRICING - timedelta(days=1), PRICING)):
    rows = []
    for name, kind, start in (("Cal-27", "year", date(2027, 1, 1)), ("Q2-27", "quarter", date(2027, 4, 1))):
        F = float(m.settle[(m.settle["profile"] == "Base") & (m.settle["name"] == name)]["settlement"].iloc[0])
        exp = last_trading_day(kind, start)
        for s in sessions:
            T = (exp - s).days / 365
            for K in np.arange(round(F) - 30, round(F) + 31, 5.0):
                rows.append({"session": s, "name": name, "kind": kind, "strike": K,
                             "call": round(black76(F, K, T, sigma, True), 3),
                             "put": round(black76(F, K, T, sigma, False), 3),
                             "call_volume": np.nan, "put_volume": np.nan,
                             "call_open_interest": 0.0, "put_open_interest": 0.0})
    return pd.DataFrame(rows)


def _history(m, n=120, sigma=HIST, seed=3):
    rng = np.random.default_rng(seed)
    F = float(m.settle[(m.settle["profile"] == "Base") & (m.settle["name"] == "Cal-27")]["settlement"].iloc[0])
    days = [d for d in (PRICING - timedelta(days=i) for i in range(400)) if is_trading_day(d)][:n + 1][::-1]
    px = [F]
    for a, b in zip(days[:-1], days[1:]):
        px.append(px[-1] * math.exp(rng.normal(0, sigma * math.sqrt((b - a).days / 365))))
    px = np.array(px) * F / px[-1]                       # dernier settlement = celui de la séance
    return pd.DataFrame({"session": days, "name": "Cal-27", "profile": "Base", "settlement": px})


def option_market(n_hist=120, sigma=SIGMA):
    m = market()
    opts, hist = _options_frame(m, sigma), _history(m, n_hist)
    return replace(m, options_loader=lambda: opts, history_loader=lambda: hist)


def test_calendar_rules():
    assert easter(2027) == date(2027, 3, 28) and not is_trading_day(date(2027, 3, 29))     # lundi de Pâques
    assert not is_trading_day(date(2026, 12, 24)) and is_trading_day(date(2026, 12, 23))
    assert last_trading_day("year", date(2027, 1, 1)) == date(2026, 12, 10)                 # 2e jeudi de décembre
    assert last_trading_day("month", date(2027, 1, 1)) == date(2026, 12, 17)                # 3e jeudi de décembre
    assert last_trading_day("quarter", date(2027, 1, 1)) == date(2026, 12, 17)
    assert last_trading_day("month", date(2026, 12, 1)) == date(2026, 11, 25)               # 4 jours de bourse avant
    assert last_trading_day("quarter", date(2027, 4, 1)) == date(2027, 3, 24)               # Pâques sautée


def test_black76_parity_greeks_implied():
    F, K, T, s = 121.35, 120.0, 45 / 365, 0.3106
    c, p = black76(F, K, T, s, True), black76(F, K, T, s, False)
    assert c - p == pytest.approx(F - K)
    assert implied_vol(p, F, K, T, False) == pytest.approx(s, abs=1e-8)
    assert black76(F, K, T, s, False) == pytest.approx(4.601, abs=0.01)                     # Cal-27 réel du 26/10
    g = greeks(F, K, T, s, True)
    h = 1e-4
    fd_delta = (black76(F + h, K, T, s, True) - black76(F - h, K, T, s, True)) / (2 * h)
    assert g["delta"] == pytest.approx(fd_delta, abs=1e-6)
    assert g["vega"] == pytest.approx((black76(F, K, T, s + 0.005, True) - black76(F, K, T, s - 0.005, True)), rel=1e-3)
    assert implied_vol(0.0, F, K, T, False) is None


def test_smile_recovers_vol_and_checks():
    m = option_market()
    opts = m.options()
    F = float(m.settle[(m.settle["profile"] == "Base") & (m.settle["name"] == "Cal-27")]["settlement"].iloc[0])
    sm = build_smile(opts, "Cal-27", "year", date(2027, 1, 1), date(2027, 12, 31), PRICING, F,
                     opts[(opts["name"] == "Cal-27") & (opts["session"] == PRICING - timedelta(days=1))])
    near = sm.table[(sm.table["strike"] - F).abs() < 15]
    assert np.allclose(near["vol"], SIGMA, atol=2e-3)                    # arrondi au millième des primes
    assert sm.checks["put-call parity max residual €/MWh"] <= 0.002
    assert abs(sm.checks["forward − settlement €/MWh"]) < 0.011
    assert sm.checks["calls decreasing in strike"] and sm.checks["calls convex in strike"]
    assert sm.checks["unchanged vs previous session"] is False
    assert any("no traded volume" in w for w in sm.warnings)
    assert sm.vol_at(F + 100)[1].startswith("flat extrapolation above")
    with pytest.raises(ValueError):                                       # échue
        build_smile(opts, "Cal-27", "year", date(2027, 1, 1), date(2027, 12, 31), date(2026, 12, 10), F)


def test_stale_settlements_flagged():
    m = market()
    o = _options_frame(m)
    o.loc[o["session"] == PRICING, ["call", "put"]] = o.loc[o["session"] == PRICING - timedelta(days=1),
                                                             ["call", "put"]].to_numpy()
    F = float(m.settle[(m.settle["profile"] == "Base") & (m.settle["name"] == "Cal-27")]["settlement"].iloc[0])
    sm = build_smile(o, "Cal-27", "year", date(2027, 1, 1), date(2027, 12, 31), PRICING, F,
                     o[(o["name"] == "Cal-27") & (o["session"] == PRICING - timedelta(days=1))])
    assert sm.checks["unchanged vs previous session"] and any("identical to the previous" in w for w in sm.warnings)


def test_historical_vol_estimate_and_minimum():
    m = option_market()
    h = historical_vol(m.futures_history(), "Cal-27", PRICING, 60)
    assert h["returns"] == 120 and h["sigma"] == pytest.approx(HIST, abs=3 * h["standard error"])
    short = historical_vol(m.futures_history().tail(40), "Cal-27", PRICING, 60)
    assert short["sigma"] is None and "minimum 60" in short["reason"] and short["sigma estimate"] > 0


def test_quote_call_sale_and_collar():
    m, s = option_market(), settings(option_fee=0.01, option_vol_premium_pts=2.0, option_min_returns=60,
                                      margin_option_base=0.05, margin_option_floor=50.0)
    assert set(option_series(m)["name"]) == {"Cal-27", "Q2-27"}
    q = quote_option(m, "call", "Cal-27", 1.0, "sell", s, strike=130.0)
    leg = q.option["legs"].iloc[0]
    assert leg["EEX vol %"] == pytest.approx(30, abs=0.3)
    assert leg["vol charged %"] == pytest.approx(leg["EEX vol %"] + 2)
    V = q.volume
    assert V == 8760
    assert q.price == pytest.approx(leg["premium charged €/MWh"] + (0.01 * V + max(0.05 * V, 50)) / V)
    parts = q.lines.drop(index=q.lines.index[[0, -1]])["€"].sum() + q.lines["€"].iloc[0]
    assert parts == pytest.approx(q.price * V)
    assert q.option["comparison"]["premium historical vol €/MWh"] > q.option["comparison"]["premium EEX vol €/MWh"]
    assert q.option["legs"]["delta (client)"].iloc[0] > 0
    c = quote_option(m, "collar", "Cal-27", 2.0, "sell", s, strike_low=100.0, strike_high=130.0)
    assert list(c.option["legs"]["client position"]) == [1, -1]
    lg = c.option["legs"]
    assert lg.loc["call 130", "vol charged %"] > lg.loc["call 130", "EEX vol %"]          # nous vendons le cap
    assert lg.loc["put 100", "vol charged %"] < lg.loc["put 100", "EEX vol %"]            # nous achetons le floor
    with pytest.raises(ValueError):
        quote_option(m, "collar", "Cal-27", 1.0, "sell", s, strike_low=130.0, strike_high=100.0)
    with pytest.raises(LookupError, match="options.frais_eur_mwh"):
        quote_option(m, "call", "Cal-27", 1.0, "sell", settings(), strike=130.0)


def test_option_quote_journal_and_archive():
    import json
    import tempfile
    from datetime import datetime
    from pathlib import Path

    from pricer.archive import save_quote
    from pricer.journal import pricing_files
    m, s = option_market(n_hist=30), settings(option_fee=0.01, option_vol_premium_pts=2.0, option_min_returns=60,
                                               margin_option_base=0.05, margin_option_floor=50.0)
    q = quote_option(m, "put", "Q2-27", 1.0, "buy", s, strike=90.0)
    assert "not available" in q.option["comparison"]["historical vol"]
    files, row = pricing_files(m, q, "Floor test", now=datetime(2026, 10, 28, 9, tzinfo=UTC))
    rec = json.loads(next(iter(files.values())))
    assert rec["option"]["series"] == "Q2-27" and len(rec["option"]["legs"]) == 1
    with tempfile.TemporaryDirectory() as tmp:
        out = save_quote(m, q, "Floor test", root=Path(tmp))
        assert (out / "option_legs.csv").exists() and (out / "option_smile.csv").exists()
