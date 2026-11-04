import tempfile
from datetime import date
from pathlib import Path

import pytest

from pricer import rates
from pricer.archive import load_position, save_quote
from pricer.products import grid_from_hours, weekly
from pricer.quote import quote
from pricer.settings import Settings
from pricer.tbx import battery, tbx_mean
from pricer.valuation import value
from tests.helpers import market, settings


def test_config_loads_and_lists_missing_parameters():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "pricer.yaml").write_text(
        "paiements: {sourcing_jours: 5, client_jours: 50, base_jours: 360}\n"
        "financement: {spread_bp: null}\n"
        "frais: {eex_eur_mwh: 0.0125, epex_eur_mwh: 0.015, conversion_fin_phys_eur_mwh: null}\n"
        "risque: {confiance: 0.95, fenetre_mois: 1, correlation_mois: 0.0, historique_debut: 2019-01-01}\n"
        "couverture: {strategie: auto}\n"
        "liquidite: {seuil_mw: 10, eur_mwh_par_mw: 0.005}\n", encoding="utf-8")
    (tmp / "marge.yaml").write_text("base_eur_mwh: 1\nforme_pct: 10\nduree_eur_mwh_par_an: 0\nplancher_eur: null\n",
                                    encoding="utf-8")
    s = Settings.load(tmp)
    assert s.sourcing_days == 5 and s.client_days == 50 and s.confidence == 0.95
    missing = s.missing(need_financing=True, manual_margin=False)
    assert any("spread_bp" in x for x in missing) and any("plancher_eur" in x for x in missing)
    assert any("conversion_fin_phys" in x for x in missing)
    assert not s.missing(need_financing=False, manual_margin=True, physical=False)
    (tmp / "pricer.yaml").write_text((tmp / "pricer.yaml").read_text().replace("auto", "base+peak fin"))
    with pytest.raises(ValueError):
        Settings.load(tmp)


def test_archive_roundtrip():
    m = market()
    p = weekly(m, date(2027, 1, 1), date(2027, 2, 28), 3, grid_from_hours(range(8, 18), range(5)), "sell")
    old, rates.estr = rates.estr, lambda d: (date(2026, 10, 12), 1.9)
    try:
        q = quote(m, p, settings())
    finally:
        rates.estr = old
    with tempfile.TemporaryDirectory() as tmp:
        path = save_quote(m, q, "Test archive", root=Path(tmp))
        rec, p2 = load_position(path)
        assert p2.mw.equals(p.mw) and p2.physical
        assert abs(rec["offer_price"] - q.price) < 1e-9
        with pytest.raises(FileExistsError):
            save_quote(m, q, "Test archive", root=Path(tmp))


def test_tbx_and_battery():
    m = market()
    tb = tbx_mean(m.price, m.tz)
    assert tb["TB1"] >= tb["TB2"] >= tb["TB4"] > 0
    bat, days = battery(m, date(2027, 1, 1), date(2027, 12, 31), 1, 2, 0.85)
    assert abs(-value(m, bat, 0.0) - days["marge €"].sum()) < 1e-6
