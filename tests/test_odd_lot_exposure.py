"""Prime de lot incomplet (lot EEX de 1 MW) et exposition au spot de chaque stratégie (aucun accès réseau)."""
from datetime import date

import pandas as pd

from tests.helpers import market, settings
from pricer import rates
from pricer.hedging import Hedge
from pricer.products import grid_from_hours, weekly
from pricer.quote import odd_lot_mwh, quote


def test_odd_lot_mwh_counts_only_the_fraction_below_a_full_lot():
    t = pd.DataFrame({"Base MW": [2.5, -1.0, 0.0], "Base MWh": [250.0, -100.0, 0.0],
                      "Peak MW": [0.25, 0.0, -3.0], "Peak MWh": [10.0, 0.0, -120.0]}, index=["a", "b", "c"])
    h = Hedge("x", t, pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=bool))
    # a : 0,5 MW sur 2,5 → 50 MWh ; Peak 0,25 MW (moins d'un lot) → 10 MWh ; b et c : lots entiers
    assert abs(odd_lot_mwh(h, 1.0) - 60.0) < 1e-9


def test_odd_lot_premium_and_spot_exposure_in_quote():
    old, rates.estr = rates.estr, lambda d: (date(2026, 10, 12), 1.90)
    try:
        m = market()
        p = weekly(m, date(2027, 1, 1), date(2027, 2, 28), 2.5, grid_from_hours(range(8, 18), range(5)), "sell")
        q0 = quote(m, p, settings(), strategy="Base+Peak monthly")
        q1 = quote(m, p, settings(odd_lot_eur_mwh=0.20), strategy="Base+Peak monthly")
        st = q1.strategies
        odd = st.loc["Base+Peak monthly", "odd-lot MWh"]
        assert odd > 0 and abs(odd - odd_lot_mwh(q1.hedge, 1.0)) < 1e-9
        assert abs((q1.price - q0.price) * q1.volume - 0.20 * odd) < 1e-6
        line = [i for i in q1.lines.index if i.startswith("+ Odd-lot premium")]
        assert line and abs(q1.lines.loc[line[0], "€"] - 0.20 * odd) < 1e-6
        for col in ("day-ahead bought MWh", "day-ahead sold MWh", "spot exposure % of volume",
                    "P&L for spot +10 €/MWh €"):
            assert col in st.columns
        r = st.loc["Base+Peak monthly"]
        assert abs(r["P&L for spot +10 €/MWh €"] - 10 * (r["day-ahead sold MWh"] - r["day-ahead bought MWh"])) < 1e-6
    finally:
        rates.estr = old
