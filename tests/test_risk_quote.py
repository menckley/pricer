from datetime import date

import numpy as np
import pandas as pd
import pytest

from pricer import rates
from pricer.hedging import hedge
from pricer.products import as_swap, block, eex, grid_from_hours, weekly
from pricer.quote import financing_cost, quote
from pricer.risk import history_bounds, residual_risk
from pricer.valuation import fair_price
from tests.helpers import market, settings


def _estr(day):
    return date(2026, 10, 12), 1.90


def _quote(*a, **k):
    old, rates.estr = rates.estr, _estr
    try:
        return quote(*a, **k)
    finally:
        rates.estr = old


def test_history_bounds():
    m = market()
    assert history_bounds(m, date(2019, 1, 1)) == (date(2019, 1, 1), date(2026, 9, 30))


def test_risk_zero_for_eex_product_positive_for_shape():
    m, s = market(), settings()
    spot = m.spot(*history_bounds(m, s.history_start))
    r0 = residual_risk(m, hedge(m, eex(m, "Cal-27")), spot)
    assert r0.cost == 0
    p = weekly(m, date(2027, 1, 1), date(2027, 3, 31), 5, grid_from_hours(range(6, 22), range(5)), "sell")
    r = residual_risk(m, hedge(m, p), spot)
    assert r.cost > 0 and len(r.by_month) >= 3
    assert (r.by_month["samples"] >= 19).all()
    # linéarité : deux fois le volume, deux fois le risque ; corrélation 1 : somme des σ
    r2 = residual_risk(m, hedge(m, 2 * p), spot)
    assert abs(r2.cost - 2 * r.cost) < 1e-6
    r1 = residual_risk(m, hedge(m, p), spot, correlation=1.0)
    assert abs(r1.sigma - r.by_month["σ €"].sum()) < 1e-6


def test_quote_sale_adds_costs_purchase_subtracts():
    m, s = market(), settings()
    g = grid_from_hours(range(6, 22), range(5))
    sale = weekly(m, date(2027, 1, 1), date(2027, 3, 31), 5, g, "sell")
    q = _quote(m, sale, s)
    assert q.side == "sell" and q.price > q.fair_price
    assert abs(q.fair_price - fair_price(m, sale)) < 1e-9
    assert abs(q.lines.loc["= Offer price", "€/MWh"] - q.price) < 1e-9
    parts = q.lines.drop(index=["= Fair price", "= Offer price"])["€"].sum()
    assert abs(parts - q.price * q.volume) < 1e-6
    assert len(q.financing) == 3 and q.financing["financing cost €"].gt(0).all()
    buy = weekly(m, date(2027, 1, 1), date(2027, 3, 31), 5, g, "buy")
    qb = _quote(m, buy, s)
    assert qb.price < qb.fair_price and len(qb.financing) == 0


def test_financing_formula():
    m, s = market(), settings()
    p = block(m, date(2027, 1, 1), date(2027, 1, 31), 1, "sell")
    f = financing_cost(m, p, s, 3.4)
    row = f.loc["2027-01"]
    assert row["sourcing paid"] == date(2027, 1, 6) and row["client pays"] == date(2027, 2, 20)
    assert abs(row["financing cost €"] - row["sourcing cost €"] * 0.034 * 45 / 360) < 1e-9


def test_swap_has_no_financing_and_margin_rules():
    m, s = market(), settings()
    p = as_swap(block(m, date(2027, 1, 1), date(2027, 12, 31), 1, "sell"))
    q = _quote(m, p, s)
    assert len(q.financing) == 0
    years_extra = 0.0
    assert abs(q.margin.eur - max(q.volume * (1.0 + 0.5 * years_extra), 500.0)) < 1e-6


def test_manual_margin_and_missing_parameters():
    m = market()
    p = block(m, date(2027, 1, 1), date(2027, 1, 31), 1, "sell")
    with pytest.raises(ValueError):
        _quote(m, p, settings(), manual_margin_eur_mwh=2.0, manual_reason="")
    q = _quote(m, p, settings(), manual_margin_eur_mwh=2.0, manual_reason="client stratégique")
    assert abs(q.margin.eur - 2.0 * q.volume) < 1e-9 and q.margin.rule == "manual"
    with pytest.raises(LookupError):
        _quote(m, p, settings(spread_bp=None))
    with pytest.raises(LookupError):
        _quote(m, p, settings(margin_floor=None))
    _quote(m, block(m, date(2027, 1, 1), date(2027, 1, 31), 1, "buy"), settings(spread_bp=None))   # pas de financement à l'achat


def test_mixed_product_refused_and_strategy_comparison():
    m, s = market(), settings()
    a = block(m, date(2027, 1, 1), date(2027, 3, 31), 1, "buy")
    b = block(m, date(2027, 4, 1), date(2027, 6, 30), 1, "sell")
    with pytest.raises(ValueError):
        _quote(m, a + b, s)
    q = _quote(m, block(m, date(2027, 1, 10), date(2027, 3, 20), 2, "sell", "Peak"), s)
    assert set(q.strategies.index) == {"Base+Peak monthly", "Base+Peak quarterly", "Base only monthly"}
    assert q.strategies.loc["Base+Peak monthly", "risk cost €"] < q.strategies.loc["Base only monthly", "risk cost €"]


def test_auto_strategy_minimises_day_ahead_volume_and_conversion_cost():
    m, s = market(), settings()
    p = weekly(m, date(2027, 1, 1), date(2027, 6, 30), 5, grid_from_hours(range(6, 22), range(5)), "sell")
    q = _quote(m, p, s)
    sel = q.strategies[q.strategies["selected"]]
    assert len(sel) == 1 and sel["day-ahead MWh"].iloc[0] == q.strategies["day-ahead MWh"].min()
    conv = q.lines.loc["+ Financial-to-physical conversion", "€"]
    assert abs(conv - 0.095 * q.volume) < 1e-9
    qs = _quote(m, as_swap(p), s)
    assert qs.lines.loc["+ Financial-to-physical conversion", "€"] == 0
    q2 = _quote(m, p, s, strategy="Base only monthly")
    assert q2.strategies.loc["Base only monthly", "selected"] and q2.price > q.price


def test_warning_for_unhedgeable_current_month():
    m, s = market(), settings()
    p = block(m, date(2026, 10, 14), date(2026, 11, 30), 1, "sell")
    q = _quote(m, p, s)
    assert q.hedge_values["unhedgeable MWh (no quoted month/quarter/year)"] > 0
    assert any("without any EEX hedge" in w for w in q.warnings)


def test_listed_contract_is_its_own_hedge():
    m, s = market(), settings()
    for p in (eex(m, "Q1-27", "Peak", 5, "sell"), block(m, date(2027, 1, 1), date(2027, 12, 31), 2, "buy")):
        q = _quote(m, p, s, strategy="Base only monthly")          # stratégie ignorée : contrat coté
        assert q.context["strategy selected"] == "Listed EEX contract"
        assert len(q.hedge.table) == 1 and q.risk.cost == 0 and len(q.strategies) == 0
        assert q.hedge_values["day-ahead surplus to sell MWh"] == 0 == q.hedge_values["day-ahead shortfall to buy MWh"]
        assert abs(q.fair_price - q.hedge.table["settlement"].iloc[0]) < 1e-12
    # un bloc qui n'est pas un contrat coté reste couvert par stratégie
    q = _quote(m, block(m, date(2027, 1, 10), date(2027, 2, 20), 1, "sell"), s)
    assert q.context["strategy selected"] != "Listed EEX contract"
