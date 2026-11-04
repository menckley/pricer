"""Prime de niveau par volatilité de marché (implicite EEX, historique) et facturation au coût du capital."""
import math
from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from pricer import rates
from pricer.deviation import build_model, market_premium
from pricer.option_quote import quote_option
from pricer.products import block
from pricer.quote import month_contract, quote, risk_charge, vol_provider
from pricer.risk import history_bounds
from tests.helpers import settings
from tests.test_options import HIST, SIGMA, option_market


def _q(*a, **k):
    old, rates.estr = rates.estr, lambda d: (date(2026, 10, 12), 1.90)
    try:
        return quote(*a, **k)
    finally:
        rates.estr = old


def test_month_contract_from_finest_to_widest():
    m = option_market()
    assert [n for n, _ in month_contract(m, "2027-01")] == ["M01-27", "Q1-27", "Cal-27"]
    assert [n for n, _ in month_contract(m, "2027-05")] == ["Q2-27", "Cal-27"]


def test_implied_and_historical_level_premium():
    m, s = option_market(), settings()
    p = block(m, date(2027, 1, 1), date(2027, 6, 30), 1, "sell")
    imp = market_premium(m, p.mw, vol_provider(m, s, "implicite"), 0.95, None, "implicite")
    jan, may = imp.by_month.loc["2027-01"], imp.by_month.loc["2027-05"]
    assert jan["source"] == "EEX implied ATM Cal-27" and may["source"] == "EEX implied ATM Q2-27"
    assert jan["vol %"] == pytest.approx(100 * SIGMA, abs=0.3)
    rel = math.sqrt(math.expm1((jan["vol %"] / 100) ** 2 * jan["τ years"]))
    assert jan["σ €"] == pytest.approx((abs(jan["Peak value €"]) + abs(jan["Offpeak value €"])) * rel)
    assert imp.cost == pytest.approx(1.6448536 * imp.by_month["σ €"].sum(), rel=1e-6)
    hist = market_premium(m, p.mw, vol_provider(m, replace(s, option_min_returns=60), "historique"), 0.95, None,
                          "historique")
    assert hist.by_month.loc["2027-01", "vol %"] == pytest.approx(100 * HIST, abs=8)        # 120 variations
    assert hist.by_month.loc["2027-01", "source"].startswith("historical Cal-27")


def test_fallback_to_persistence_is_flagged():
    m, s = option_market(), settings()
    spot = m.spot(*history_bounds(m, s.history_start))
    model = build_model(m, spot, 30, s.deviation_excluded, 7, 12)
    p = block(m, date(2026, 11, 1), date(2026, 11, 30), 1, "sell")          # aucune option sur M11-26 / Q4 / Cal-26
    r = market_premium(m, p.mw, vol_provider(m, s, "implicite"), 0.95, model, "implicite")
    assert r.by_month.loc["2026-11", "source"].startswith("persistence proxy") and r.cost > 0
    with pytest.raises(LookupError):
        market_premium(m, p.mw, vol_provider(m, s, "implicite"), 0.95, None, "implicite")


def test_cost_of_capital_charge():
    s = settings(risk_charging="cout_du_capital", cost_of_capital=0.10)
    bm = pd.DataFrame({"σ €": [100.0, 300.0]}, index=["2027-01", "2027-03"])
    cost, rule = risk_charge(1000.0, bm, s, date(2026, 12, 31))
    tau = (0.25 * 32 + 0.75 * 91) / 365                                     # fins de mois : 01/02 et 01/04
    assert cost == pytest.approx(0.10 * 1000 * tau) and "10 %/yr" in rule
    assert risk_charge(1000.0, bm, settings(), date(2026, 12, 31))[0] == 1000.0    # quantile entier


def test_quote_with_market_vol_and_cost_of_capital():
    m = option_market()
    p = block(m, date(2027, 1, 1), date(2027, 6, 30), 1, "sell")
    s = settings(risk_charging="cout_du_capital", cost_of_capital=0.10)
    q = _q(m, p, s, strategy="Full day-ahead", level_method="implicite")
    full = _q(m, p, settings(), strategy="Full day-ahead", level_method="implicite")
    assert q.deviation.method == "EEX implied volatility" and q.context["risk charging"] == "cost of capital"
    assert 0 < q.deviation.cost < full.deviation.cost and q.risk.cost <= full.risk.cost
    assert any("level premium (EEX implied volatility" in i for i in q.lines.index)
    parts = q.lines.drop(index=["= Fair price", "= Offer price"])["€"].sum()
    assert parts == pytest.approx(q.price * q.volume)
    with pytest.raises(LookupError, match="risque.facturation"):
        _q(m, p, settings(risk_charging=None))


def test_option_volatility_source():
    m = option_market()
    s = settings(option_fee=0.0, option_vol_premium_pts=0.0, option_min_returns=60, margin_option_base=0.0,
                 margin_option_floor=0.0)
    imp = quote_option(m, "call", "Cal-27", 1.0, "sell", s, strike=130.0, vol_source="implicite")
    his = quote_option(m, "call", "Cal-27", 1.0, "sell", s, strike=130.0, vol_source="historique")
    pru = quote_option(m, "call", "Cal-27", 1.0, "sell", s, strike=130.0, vol_source="prudente")
    assert his.price > imp.price and pru.price == pytest.approx(his.price)       # vente : la plus haute (hist 40 %)
    assert imp.fair_price == pytest.approx(his.fair_price)                        # le prix juste reste à la vol EEX
    buy = quote_option(m, "call", "Cal-27", 1.0, "buy", s, strike=130.0, vol_source="prudente")
    assert buy.price == pytest.approx(imp.price)                                  # achat : la plus basse (EEX 30 %)
    with pytest.raises(LookupError, match="historique non disponible"):
        quote_option(option_market(n_hist=20), "call", "Cal-27", 1.0, "sell", s, strike=130.0, vol_source="historique")
