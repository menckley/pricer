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
