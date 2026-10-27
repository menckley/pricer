"""Contrats physiques indexés sur le spot EPEX : « EPEX spot ± spread »."""
from datetime import date

import pandas as pd
import pytest

from pricer import rates
from pricer.indexation import ceil_cent, monthly_index, quote_indexed
from pricer.products import as_swap, block, grid_from_hours, weekly
from tests.helpers import market, settings


def _q(*a, **k):
    old, rates.estr = rates.estr, lambda d: (date(2026, 10, 12), 1.90)
    try:
        return quote_indexed(*a, **k)
    finally:
        rates.estr = old


def test_ceil_cent():
    assert ceil_cent(0.69) == 0.69 and ceil_cent(0.6901) == 0.70 and ceil_cent(41.2300000001) == 41.23
    assert ceil_cent(87.004) == 87.01 and ceil_cent(-0.004) == 0.0


def test_monthly_index_volume_weighted_and_rounded_up():
    m = market()
    g = grid_from_hours(range(8, 20), range(5))
    p = weekly(m, date(2027, 1, 1), date(2027, 2, 28), 2, g, "sell") + \
        weekly(m, date(2027, 1, 1), date(2027, 2, 28), 1, grid_from_hours(range(20, 24), range(5)), "sell")
    idx = monthly_index(m, p, m.price)
    q = p.mw.abs()
    jan = m.month[q.index] == "2027-01"
    exact = float((q[jan] * m.price[q.index][jan]).sum() / q[jan].sum())
    assert idx.loc["2027-01", "index exact €/MWh"] == pytest.approx(exact)
    assert idx.loc["2027-01", "index €/MWh (ceil to cent)"] == ceil_cent(exact) >= exact
    assert idx["MWh"].sum() == pytest.approx(q.sum())


def test_spread_build_up_sale():
    m, s = market(), settings()
    p = block(m, date(2027, 1, 1), date(2027, 12, 31), 1, "sell")
    q = _q(m, p, s)
    V = q.volume
    fin = q.financing["financing cost €"].sum()
    exact = (0.05 * V + 0.095 * V + fin + q.margin.eur) / V
    assert q.indexation["spread exact €/MWh"] == pytest.approx(exact)
    assert q.indexation["spread €/MWh"] == ceil_cent(exact)
    assert q.indexation["formula"] == f"EPEX spot day-ahead + {ceil_cent(exact):.2f} €/MWh"
    assert q.lines.loc["= Spread quoted", "€/MWh"] == pytest.approx(q.indexation["spread €/MWh"])
    assert q.lines.loc["Index rounding to the cent (paid to the producer)", "€"] == 0
    assert q.risk.cost == 0 and len(q.hedge.table) == 0
    assert q.margin.eur == pytest.approx(max(V * 0.30, 100.0))          # 1 an : pas de part durée
    assert q.price == pytest.approx(q.fair_price + q.indexation["spread €/MWh"])
    assert len(q.indexation["by_month"]) == 12


def test_purchase_from_producer_no_funding_rounding_counted():
    m, s = market(), settings()
    q = _q(m, block(m, date(2027, 1, 1), date(2027, 3, 31), 5, "buy", "Peak"), s)
    assert len(q.financing) == 0
    assert q.lines.loc["Index rounding to the cent (paid to the producer)", "€/MWh"] == pytest.approx(0.01)
    assert q.indexation["formula"].startswith("EPEX spot day-ahead − ")
    assert q.price == pytest.approx(q.fair_price - q.indexation["spread €/MWh"])


def test_manual_margin_swap_refused_and_parameters():
    m, s = market(), settings()
    p = block(m, date(2027, 1, 1), date(2027, 1, 31), 1, "sell")
    with pytest.raises(ValueError):
        _q(m, as_swap(p), s)
    with pytest.raises(ValueError):
        _q(m, p, s, manual_margin_eur_mwh=0.2)                          # motif obligatoire
    q = _q(m, p, s, manual_margin_eur_mwh=0.2, manual_reason="client historique")
    assert q.margin.eur == pytest.approx(0.2 * q.volume) and q.margin.rule == "manual"
    with pytest.raises(LookupError, match="indexation.base_eur_mwh"):
        _q(m, p, settings(margin_index_base=None))


def test_journal_and_archive_of_an_indexed_pricing():
    import json
    import tempfile
    from pathlib import Path
    from datetime import datetime, timezone
    from pricer.archive import save_quote
    from pricer.journal import pricing_files
    m, s = market(), settings()
    q = _q(m, block(m, date(2027, 1, 1), date(2027, 6, 30), 2, "sell"), s)
    files, row = pricing_files(m, q, "Client indexé", now=datetime(2026, 10, 27, 12, tzinfo=timezone.utc))
    rec = json.loads(next(iter(files.values())))
    assert rec["indexation"]["spread €/MWh"] == q.indexation["spread €/MWh"] and len(rec["indexation"]["by_month"]) == 6
    assert row["hedging"] == "EPEX spot indexed (no hedge)"
    with tempfile.TemporaryDirectory() as tmp:
        out = save_quote(m, q, "Client indexé", root=Path(tmp))
        assert (out / "index_by_month.csv").exists()
