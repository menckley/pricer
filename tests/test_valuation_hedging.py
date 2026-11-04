from datetime import date

import numpy as np
import pytest

from pricer.hedging import STRATEGIES, hedge, hedge_summary, partition
from pricer.products import block, eex, grid_from_hours, weekly
from pricer.valuation import decompose, fair_price, value
from tests.helpers import market


def test_market_reprices_settlements():
    check = market().check_settlements()
    assert check["ecart"].abs().max() <= 0.01


def test_fair_price_equals_settlement_and_value_zero():
    m = market()
    for r in m.settle.itertuples():
        p = eex(m, r.name, r.profile)
        assert abs(fair_price(m, p) - r.settlement) <= m.tol
    cal = eex(m, "Cal-27")
    assert abs(value(m, cal, fair_price(m, cal))) < 1e-6


def test_decompose_sums_to_value():
    m = market()
    p = weekly(m, date(2027, 1, 1), date(2027, 6, 30), 5, grid_from_hours(range(6, 22), range(5)))
    for by in ("mois", "trimestre", "année"):
        d = decompose(m, p, 100, by)
        assert abs(d.loc[("Total", ""), "valeur €"] - value(m, p, 100)) < 1e-6
        assert d.drop(index="Total")["heures"].sum() == len(p.mw)


def test_partition_finest():
    m = market()
    pieces, quoted = partition(m)
    names = [quoted[k][0] for k in pieces]
    assert names == ["M11-26", "M12-26", "M01-27", "M02-27", "M03-27", "Q2-27", "Q3-27", "Q4-27", "Cal-28"]
    pieces, quoted = partition(m, ("trimestre", "année"))
    assert [quoted[k][0] for k in pieces] == ["Q1-27", "Q2-27", "Q3-27", "Q4-27", "Cal-28"]


def test_eex_product_hedges_without_residual():
    m = market()
    for p in (eex(m, "Cal-27"), eex(m, "Q1-27", "Peak", 3, "sell"), eex(m, "Cal-28")):
        h = hedge(m, p)
        assert h.residual.abs().sum() < 1e-6


def test_residual_zero_per_block_and_values_add_up():
    m = market()
    p = weekly(m, date(2027, 1, 1), date(2027, 6, 30), 5, grid_from_hours(range(6, 22), range(5)), "sell")
    for strat, spec in STRATEGIES.items():
        h = hedge(m, p, strat)
        pieces, _ = partition(m, spec["levels"])
        for s, e in pieces:
            mm = (m.day >= s) & (m.day <= e)
            if spec.get("peak_only"):
                blocks = (mm & m.is_peak,)                    # offpeak entièrement en day-ahead
            else:
                blocks = (mm & m.is_peak, mm & ~m.is_peak) if spec["peak"] else (mm,)
            for b in blocks:
                assert abs(h.residual[b].sum()) < 1e-6
        v = hedge_summary(m, p, h)
        assert abs(v["settlement rounding €"]) <= m.tol * v["EEX hedged MWh"] + 1e-6
        assert abs(h.surplus_mwh - h.deficit_mwh - (p.volume - h.hedged.sum())) < 1e-6


def test_base_only_hedge_has_more_residual():
    m = market()
    p = block(m, date(2027, 1, 1), date(2027, 3, 31), 10, "buy", "Peak")
    assert hedge(m, p, "Base+Peak monthly").residual.abs().sum() < 1e-6
    assert hedge(m, p, "Base only monthly").residual.abs().sum() > 1000


def test_peak_only_hedges_peak_mwh_and_leaves_offpeak():
    from pricer.hedging import level_open
    m = market()
    g = grid_from_hours(range(8, 20), range(7))                          # 8h-20h tous les jours
    p = weekly(m, date(2027, 1, 1), date(2027, 3, 31), 1, g, "sell")
    h = hedge(m, p, "Peak only + day-ahead")
    assert (h.table["Base MW"] == 0).all() and np.allclose(h.table["Peak MW"], -1.0)
    pk = m.is_peak[p.mw.index]
    assert abs(h.residual[p.mw.index][pk].sum()) < 1e-6                  # Peak : couvert à l'identique
    lo = level_open(m, p, h)
    assert np.isclose(lo.sum(), p.mw[~pk].sum()) and len(lo) == (~pk).sum()
    assert level_open(m, p, hedge(m, p, "Base+Peak monthly")).empty


def test_coverage_of_the_extended_peak_profile():
    """1 MW 8h-20h 7j/7 : Base = 24/108 MW, Peak = 1 − Base ; week-end acheté, nuits revendues, en volumes égaux."""
    from pricer.hedging import coverage, typical_week
    m = market()
    g = grid_from_hours(range(8, 20), range(7))
    p = weekly(m, date(2027, 1, 1), date(2027, 3, 31), 1, g, "sell")
    h = hedge(m, p, "Base+Peak monthly")
    cov = coverage(m, p, h)
    wk = typical_week(m, cov, "EEX Base MW", "2027-01")
    jan = h.table.loc["M01-27"]
    assert np.allclose(wk.to_numpy(), -jan["Base MW"])                    # … égal au MW du contrat (vente : signe −)
    buy, sell = typical_week(m, cov, "day-ahead purchase MW"), typical_week(m, cov, "day-ahead resale MW")
    assert (buy.loc["12h-13h", ["Sat", "Sun"]] > 0.7).all() and (buy.loc["12h-13h", ["Mon", "Fri"]] < 1e-9).all()
    assert (sell.loc["02h-03h"] > 0.2).all() and (sell.loc["12h-13h"] < 1e-9).all()
    assert abs(cov["day-ahead purchase MW"].sum() - cov["day-ahead resale MW"].sum()) < 1e-6
    d = typical_week(m, cov, "delivered MW")
    assert d.loc["12h-13h", "Sun"] == pytest.approx(1.0) and d.loc["02h-03h", "Mon"] == 0
    # contrat coté couvert par lui-même et full day-ahead
    from pricer.hedging import full_da_hedge, listed_contract, listed_hedge
    from pricer.products import eex
    q = eex(m, "Q1-27", "Peak", 2, "buy")
    c = coverage(m, q, listed_hedge(m, q, listed_contract(m, q)))
    assert np.allclose(c["EEX Peak MW"], 2.0) and c["day-ahead purchase MW"].abs().sum() == 0
    f = coverage(m, p, full_da_hedge(m, p))
    assert np.allclose(f["day-ahead purchase MW"], f["delivered MW"]) and f["EEX Base MW"].abs().sum() == 0
