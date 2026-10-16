"""Prime de déviation (persistance) et scénario Full day-ahead."""
from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import pytest

from pricer import rates
from pricer.deviation import build_model, deviation_premium, months_ahead
from pricer.hedging import FULL_DA
from pricer.products import block, eex, grid_from_hours, weekly
from pricer.quote import quote, shape_value
from pricer.risk import history_bounds
from tests.helpers import TZ, hours, market, peak, settings

CRISIS = ((date(2021, 9, 1), date(2023, 6, 30)),)


def _quote(*a, **k):
    old, rates.estr = rates.estr, lambda d: (date(2026, 10, 12), 1.90)
    try:
        return quote(*a, **k)
    finally:
        rates.estr = old


def _model(m=None, excluded=CRISIS, min_months=12):
    m = m or market()
    spot = m.spot(*history_bounds(m, date(2019, 1, 1)))
    return m, build_model(m, spot, 30, excluded, 7, min_months)


def test_horizons_and_excluded_crisis():
    m, mod = _model()
    e = mod.errors
    assert set(mod.seasonal.index) == set(range(1, 13))
    assert e["k"].min() == 0 and e["k"].max() > 24
    origins = pd.to_datetime(e["origin"]).dt.date
    assert not ((origins >= date(2021, 9, 1)) & (origins <= date(2023, 6, 30))).any()
    t = e["target month"]
    assert not ((t >= "2021-09") & (t <= "2023-06")).any()
    # origines à ± 7 jours du jour du mois de la date de pricing (13)
    assert pd.to_datetime(e["origin"]).dt.day.between(6, 20).all()
    r1, n1, months = mod.rmse(1, "Peak")
    assert r1 > 0 and months >= 12
    tab = mod.table(3)
    assert list(tab.index) == [0, 1, 2, 3] and tab["Peak RMSE %"].notna().all()
    # sans exclusion, la crise revient dans les cibles
    _, mod_all = _model(excluded=())
    assert (mod_all.errors["target month"] == "2022-08").any()


def test_exact_seasonality_gives_zero_error():
    """Spot constant par (mois calendaire, bloc), identique chaque année : la persistance saisonnière est exacte."""
    base = market()
    idx = hours(date(2019, 1, 1), date(2026, 9, 30))
    loc = idx.tz_convert(TZ)
    a = 50 + 20 * np.cos((loc.month.to_numpy() - 1) / 12 * 2 * np.pi)
    s = pd.Series(np.where(peak(idx), 1.3 * a, a), index=idx)
    m = replace(base, spot_loader=lambda d0, d1: s[(s.index.tz_convert(TZ).date >= d0)
                                                    & (s.index.tz_convert(TZ).date <= d1)])
    _, mod = _model(m)
    assert mod.errors["error"].abs().max() < 1e-9
    p = block(m, date(2026, 10, 14), date(2027, 6, 30), 3, "sell")
    assert deviation_premium(m, p.mw, mod, 0.95).cost < 1e-6


def test_premium_formula_and_linearity():
    m, mod = _model()
    p = block(m, date(2026, 10, 14), date(2027, 2, 28), 2, "sell")
    d = deviation_premium(m, p.mw, mod, 0.95)
    assert list(d.by_month["horizon (months)"]) == [0, 1, 2, 3, 4]
    row = d.by_month.loc["2027-01"]
    v_pk = float((p.mw * m.price[p.mw.index])[(m.month[p.mw.index] == "2027-01") & m.is_peak[p.mw.index]].sum())
    assert row["Peak value €"] == pytest.approx(v_pk)
    r_pk, _, _ = mod.rmse(3, "Peak")
    r_op, _, _ = mod.rmse(3, "Offpeak")
    assert row["σ €"] == pytest.approx(abs(row["Peak value €"]) * r_pk + abs(row["Offpeak value €"]) * r_op)
    assert d.sigma == pytest.approx(d.by_month["σ €"].sum())          # mois parfaitement corrélés
    d2 = deviation_premium(m, 2 * p.mw, mod, 0.95)
    assert d2.cost == pytest.approx(2 * d.cost)
    assert months_ahead(date(2026, 10, 13), "2028-01") == 15


def test_too_short_history_refused():
    m, mod = _model(min_months=10_000)
    p = block(m, date(2027, 1, 1), date(2027, 1, 31), 1, "sell")
    with pytest.raises(LookupError):
        deviation_premium(m, p.mw, mod, 0.95)


def test_full_day_ahead_scenario_in_comparison():
    m, s = market(), settings()
    p = weekly(m, date(2027, 1, 1), date(2027, 3, 31), 5, grid_from_hours(range(6, 22), range(5)), "sell")
    q = _quote(m, p, s)
    fda = q.strategies.loc[FULL_DA]
    assert fda["EEX contracts"] == 0 and fda["EEX hedged MWh"] == 0 and fda["liquidity €"] == 0
    assert fda["day-ahead MWh"] == pytest.approx(q.volume)
    assert fda["deviation premium €"] > 0
    assert fda["fees + conversion €"] == pytest.approx(0.05 * q.volume)      # day-ahead seulement, pas de conversion
    # même marge dans tous les scénarios : la prime de forme ne dépend pas de la couverture
    assert q.strategies["margin €"].nunique() == 1
    # scénario forcé
    qf = _quote(m, p, s, strategy=FULL_DA)
    assert qf.context["strategy selected"] == FULL_DA
    assert "Day-ahead sourcing at HPFC (no EEX hedge)" in qf.lines.index
    assert "EEX hedge at settlement" not in qf.lines.index
    parts = qf.lines.drop(index=["= Fair price", "= Offer price"])["€"].sum()
    assert parts == pytest.approx(qf.price * qf.volume)
    assert qf.fair_price == pytest.approx(q.fair_price)
    assert qf.lines.loc["+ Financial-to-physical conversion", "€"] == 0
    assert qf.price == pytest.approx(fda["offer price €/MWh"])
    assert any("no EEX hedge" in w for w in qf.warnings)


def test_full_day_ahead_wins_when_cheapest():
    """Day-ahead gratuit, EEX très cher : le full day-ahead devient le moins cher et est retenu en auto."""
    m = market()
    s = settings(eex_fee=500.0, epex_fee=0.0)
    p = weekly(m, date(2026, 11, 1), date(2026, 11, 30), 1, grid_from_hours(range(9, 19), range(5)), "buy")   # pas le Peak coté
    q = _quote(m, p, s)
    assert q.context["strategy selected"] == FULL_DA
    assert q.strategies.loc[FULL_DA, "offer price €/MWh"] == pytest.approx(q.strategies["offer price €/MWh"].max())


def test_full_day_ahead_disabled_or_not_measurable():
    m = market()
    p = block(m, date(2027, 1, 10), date(2027, 3, 20), 2, "sell", "Peak")
    q = _quote(m, p, settings(full_day_ahead=False))
    assert FULL_DA not in q.strategies.index
    with pytest.raises(ValueError):
        _quote(m, p, settings(full_day_ahead=False), strategy=FULL_DA)
    # horizon au-delà de l'historique mesurable : scénario non évalué, signalé, les autres restent
    q = _quote(m, block(m, date(2028, 9, 10), date(2028, 11, 20), 1, "sell"), settings(deviation_min_months=40))
    assert FULL_DA not in q.strategies.index
    assert any(w.startswith(f"{FULL_DA} not evaluated") for w in q.warnings)


def test_deviation_parameters_required_for_profiled_products_only():
    m = market()
    s = settings(deviation_lookback=None)
    with pytest.raises(LookupError, match="deviation.fenetre_jours"):
        _quote(m, block(m, date(2027, 1, 10), date(2027, 2, 20), 1, "sell"), s)
    q = _quote(m, eex(m, "Cal-27", "Base", 1, "sell"), s)                  # contrat coté : pas de day-ahead
    assert q.deviation.cost == 0


def test_shape_value_is_zero_for_flat_blocks():
    m = market()
    assert abs(shape_value(m, block(m, date(2027, 1, 1), date(2027, 3, 31), 3, "sell", "Peak"))) < 1e-6
    p = weekly(m, date(2027, 1, 1), date(2027, 1, 31), 1, grid_from_hours(range(18, 22), range(5)), "buy")
    assert shape_value(m, p) != 0
