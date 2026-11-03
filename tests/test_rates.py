"""€STR de la BCE : nouveaux essais sur panne passagère, erreur explicite, cache (aucun accès réseau)."""
from datetime import date

import pytest
import requests

from pricer import rates


class R:
    def __init__(self, code, text=""):
        self.status_code, self.text, self.ok = code, text, 200 <= code < 300


CSV = "KEY,TIME_PERIOD,OBS_VALUE\nEST.B,2026-10-29,1.93\n"


def _patch(responses):
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(params)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    return get, calls


def test_retry_then_success_and_cache(monkeypatch=None):
    old_get, old_sleep = requests.get, rates.time.sleep
    get, calls = _patch([R(504), requests.ConnectionError("x"), R(200, CSV)])
    requests.get, rates.time.sleep = get, lambda s: None
    rates._estr_cached.cache_clear()
    try:
        assert rates.estr(date(2026, 10, 30)) == (date(2026, 10, 29), 1.93)
        assert len(calls) == 3
        assert rates.estr(date(2026, 10, 30)) == (date(2026, 10, 29), 1.93) and len(calls) == 3   # cache
    finally:
        requests.get, rates.time.sleep = old_get, old_sleep
        rates._estr_cached.cache_clear()


def test_persistent_outage_gives_clear_error():
    old_get = requests.get
    get, calls = _patch([R(504)] * 4)
    requests.get = get
    rates._estr_cached.cache_clear()
    try:
        with pytest.raises(LookupError, match="BCE indisponible .HTTP 504."):
            rates._get_csv(rates.ESTR_KEY, {}, sleep=lambda s: None)
        assert len(calls) == 4
        get, calls = _patch([R(400)])
        requests.get = get
        with pytest.raises(LookupError, match="HTTP 400"):
            rates._get_csv(rates.ESTR_KEY, {}, sleep=lambda s: None)
        assert len(calls) == 1                                  # erreur définitive : pas de nouvel essai
    finally:
        requests.get = old_get
        rates._estr_cached.cache_clear()


def test_stale_rate_refused():
    old_get = requests.get
    get, _ = _patch([R(200, "KEY,TIME_PERIOD,OBS_VALUE\nEST.B,2026-10-20,1.9\n")])
    requests.get = get
    rates._estr_cached.cache_clear()
    try:
        with pytest.raises(LookupError, match="trop ancien"):
            rates.estr(date(2026, 10, 30))
    finally:
        requests.get = old_get
        rates._estr_cached.cache_clear()


def test_fallback_to_archive_is_flagged():
    import pandas as pd
    old = rates.estr

    def down(day):
        raise LookupError("API de la BCE indisponible (HTTP 504) : €STR non obtenu")
    journal = pd.Series([1.92, 1.93, 1.95], index=[date(2026, 10, 28), date(2026, 10, 29), date(2026, 11, 2)])
    rates.estr = down
    try:
        d, v, src, warn = rates.estr_for_pricing(date(2026, 10, 30), lambda: journal)
        assert (d, v) == (date(2026, 10, 29), 1.93) and "archive" in src
        assert "ECB API unavailable (API de la BCE indisponible (HTTP 504) " in warn and "1 day(s)" in warn
        with pytest.raises(LookupError):                       # pas de journal : erreur, aucun taux supposé
            rates.estr_for_pricing(date(2026, 10, 30), None)
        with pytest.raises(LookupError, match="aucun €STR archivé"):
            rates.estr_for_pricing(date(2026, 10, 1), lambda: journal)
    finally:
        rates.estr = old
    rates.estr = lambda day: (date(2026, 10, 29), 1.93)
    try:
        assert rates.estr_for_pricing(date(2026, 10, 30), lambda: journal)[2:] == ("ECB API", None)
    finally:
        rates.estr = old


def test_quote_shows_archive_source():
    import pandas as pd
    from dataclasses import replace
    from pricer.indexation import quote_indexed
    from pricer.products import block
    from tests.helpers import market, settings
    old = rates.estr

    def down(day):
        raise LookupError("API de la BCE indisponible (HTTP 504) : €STR non obtenu")
    m = replace(market(), estr_loader=lambda: pd.Series([1.9], index=[date(2026, 10, 12)]))
    rates.estr = down
    try:
        q = quote_indexed(m, block(m, date(2027, 1, 1), date(2027, 1, 31), 1, "sell"), settings())
        assert q.context["€STR source"].startswith("power-hpfc archive") and q.context["€STR date"] == "2026-10-12"
        assert any(w.startswith("ECB API unavailable") for w in q.warnings)
    finally:
        rates.estr = old
