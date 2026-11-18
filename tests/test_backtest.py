from datetime import date

import numpy as np
import pandas as pd
import pytest

from pricer.backtest import (
    UnsupportedPricing,
    _hours,
    backtest,
    calibrate,
    is_test,
    profile_from_spec,
    raroc_margin,
    replay,
    summarize,
    var_cvar,
)

SPEC = {"type": "block", "start": "2026-01-01", "end": "2026-01-31", "mw": 2.0, "side": "sell", "profile": "Base"}


def record(pid="p1", label="client", spec=SPEC, offer=100.0, fair=99.0, hedge_settl=90.0):
    return {"id": pid, "label": label, "offer_price": offer, "fair_price": fair, "hedging": "Base only monthly",
            "product": {"spec": spec, "delivery_end": spec["end"]},
            "hedge": [{"start": "2026-01-01", "end": "2026-01-31", "Base MW": -2.0, "Peak MW": 0.0,
                       "Base settl.": hedge_settl, "Peak settl.": hedge_settl}],
            "margin": {"eur": 150.0}, "risk": {"cost_eur": 10.0}, "deviation": {"method": "persistance"}}


def spot(price):
    return pd.Series(price, index=_hours(date(2026, 1, 1), date(2026, 1, 31)))


def test_profile_sell_is_negative_and_hour_count():
    q = profile_from_spec(SPEC)
    assert len(q) == 31 * 24 and (q == -2.0).all()
    with pytest.raises(UnsupportedPricing):
        profile_from_spec({"type": "hourly"})


def test_replay_fully_hedged_pnl_is_offer_minus_settlement():
    res = replay(record(), spot(500.0))                 # spot sans effet : couverture exacte
    assert res["volume_mwh"] == pytest.approx(2 * 744)
    assert res["pnl_eur_mwh"] == pytest.approx(10.0)
    assert res["cost_error_eur_mwh"] == pytest.approx(90.0 - 99.0)


def test_replay_unhedged_residual_uses_realized_spot():
    rec = record()
    rec["hedge"] = []
    res = replay(rec, spot(120.0))                      # vendu 100, acheté 120 en day-ahead
    assert res["pnl_eur_mwh"] == pytest.approx(-20.0)


def test_replay_requires_complete_spot():
    with pytest.raises(LookupError):
        replay(record(), spot(100.0).iloc[:-1])


def test_backtest_excludes_tests_and_undelivered():
    late = {**SPEC, "end": "2026-02-28"}
    recs = [record("a"), record("b", label="test-x"), record("c", label="zfvzaez"),
            record("d", spec=late), record("e", spec={"type": "hourly", "end": "2026-01-31"})]
    res, skipped = backtest(recs, spot(100.0), date(2026, 2, 1))
    assert list(res["id"]) == ["a"]
    assert set(skipped["id"]) == {"b", "c", "d", "e"}
    res2, _ = backtest(recs[:1], spot(100.0), date(2026, 2, 1), exclude_ids=["a"])
    assert res2.empty
    assert is_test("TEST") and not is_test("Proxelia")


def test_var_cvar_and_raroc():
    pnl = np.arange(-9, 11)                              # pertes −10…9 ; n = 20
    var, cvar = var_cvar(pnl, 0.9)
    assert cvar == pytest.approx((8 + 9) / 2)            # 2 pires pertes : 8 et 9
    assert var <= cvar
    assert raroc_margin(100.0, 0.15) == 15.0 and raroc_margin(-5.0, 0.15) == 0.0
    with pytest.raises(ValueError):
        var_cvar(pnl, 0.3)


def test_calibrate_flags_small_samples_and_summarize():
    res, _ = backtest([record(f"p{i}", fair=95.0 + i) for i in range(3)], spot(100.0), date(2026, 2, 1))
    cal = calibrate(res)
    assert cal.n == 3 and not cal.reliable
    s = summarize(res, ["strategy", "deviation_method"])
    assert s.loc[0, "n"] == 3 and not s.loc[0, "reliable"]
    assert s.loc[0, "bias_eur_mwh"] == pytest.approx(res["cost_error_eur_mwh"].mean())
    with pytest.raises(ValueError):
        calibrate(res.iloc[0:0])
