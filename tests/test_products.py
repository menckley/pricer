from datetime import date

import numpy as np
import pandas as pd
import pytest

from pricer.products import as_swap, block, eex, empty_grid, grid_from_hours, shaped, weekly
from tests.helpers import market


def test_block_hours_and_sign():
    m = market()
    cal = block(m, date(2027, 1, 1), date(2027, 12, 31), 2, "sell")
    assert len(cal.mw) == 8760 and cal.volume == -2 * 8760 and cal.side == "sell"
    pk = block(m, date(2027, 1, 1), date(2027, 12, 31), 1, "buy", "Peak")
    op = block(m, date(2027, 1, 1), date(2027, 12, 31), 1, "buy", "Offpeak")
    assert len(pk.mw) == 261 * 12 and len(pk.mw) + len(op.mw) == 8760


def test_weekly_grid_matches_shaped_and_holidays_follow_weekday():
    m = market()
    g = grid_from_hours(range(6, 22), range(5))
    a = weekly(m, date(2027, 1, 1), date(2027, 3, 31), 5, g)
    b = shaped(m, date(2027, 1, 1), date(2027, 3, 31), 5, range(6, 22), range(5))
    assert a.mw.equals(b.mw) and len(a.mw) == 64 * 16
    # vendredi 1er janvier 2027 (férié) livré comme un vendredi
    jan1 = a.mw[a.mw.index.tz_convert(m.tz).date == date(2027, 1, 1)]
    assert len(jan1) == 16


def test_weekly_dst_days():
    m = market()
    with pytest.raises(ValueError):                                 # 28/03/2027 : 23 h, pas de 2h-3h
        weekly(m, date(2027, 3, 28), date(2027, 3, 28), 1, grid_from_hours([2], [6]))
    q = weekly(m, date(2027, 10, 31), date(2027, 10, 31), 1, grid_from_hours([2], [6]))   # 25 h : deux fois
    assert len(q.mw) == 2


def test_grid_validation():
    m = market()
    with pytest.raises(ValueError):
        weekly(m, date(2027, 1, 1), date(2027, 1, 31), 1, empty_grid())
    with pytest.raises(ValueError):
        weekly(m, date(2027, 1, 1), date(2027, 1, 31), 1, np.ones((24, 6), bool))
    with pytest.raises(ValueError):
        block(m, date(2030, 1, 1), date(2030, 1, 31), 1)
    with pytest.raises(ValueError):
        block(m, date(2027, 1, 1), date(2027, 1, 31), -1)


def test_eex_and_swap():
    m = market()
    p = eex(m, "Q1-27", "Peak", 3, "sell")
    assert p.volume == -3 * len(p.mw)
    with pytest.raises(KeyError):
        eex(m, "Cal-30")
    s = as_swap(p)
    assert not s.physical and "receiver" in s.name
    with pytest.raises(ValueError):
        p + as_swap(eex(m, "Q2-27"))
