"""Taux publiés par la BCE (API publique data-api.ecb.europa.eu) : €STR."""
from __future__ import annotations

import io
import time
from datetime import date, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd
import requests

ECB_URL = "https://data-api.ecb.europa.eu/service/data/"
ESTR_KEY = "EST/B.EU000A2X2A25.WT"                 # €STR, taux moyen pondéré par les volumes
MAX_ESTR_LAG_DAYS = 5
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_WAITS = (2, 5, 10)                           # secondes entre les essais (4 essais au total)


def _get_csv(path: str, params: dict, sleep=time.sleep) -> pd.DataFrame:
    """Requête à l'API de la BCE. Panne passagère (504, 503…, réseau) : nouvel essai après 2, 5 puis 10 s ;
    toujours en panne : LookupError explicite (aucun taux n'est supposé)."""
    last = ""
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            r = requests.get(ECB_URL + path, params={**params, "format": "csvdata"}, timeout=30)
        except requests.RequestException as exc:
            last = f"erreur réseau ({exc.__class__.__name__})"
        else:
            if r.status_code == 404 or (r.ok and not r.text.strip()):
                return pd.DataFrame()
            if r.ok:
                return pd.read_csv(io.StringIO(r.text))
            last = f"HTTP {r.status_code}"
            if r.status_code not in RETRY_STATUS:
                break
        if attempt < len(RETRY_WAITS):
            sleep(RETRY_WAITS[attempt])
    raise LookupError(f"API de la BCE indisponible ({last}) : €STR non obtenu, pricing impossible pour l'instant. "
                      "Réessayer dans quelques minutes (data-api.ecb.europa.eu).")


@lru_cache(maxsize=64)
def _estr_cached(day: date) -> tuple[date, float]:
    df = _get_csv(ESTR_KEY, {"endPeriod": day.isoformat(), "lastNObservations": 1})
    if df.empty:
        raise LookupError(f"€STR introuvable à la date du {day}")
    row = df.sort_values("TIME_PERIOD").iloc[-1]
    return date.fromisoformat(str(row["TIME_PERIOD"])), float(row["OBS_VALUE"])


def estr(day: date) -> tuple[date, float]:
    """Dernier €STR publié pour un jour ≤ `day` (en %). Erreur s'il date de plus de 5 jours.
    Un taux obtenu est gardé en mémoire pour ce jour : les pricings suivants n'interrogent plus la BCE."""
    d, v = _estr_cached(day)
    if (day - d).days > MAX_ESTR_LAG_DAYS or not np.isfinite(v):
        raise LookupError(f"dernier €STR publié le {d}, trop ancien pour un pricing au {day}")
    return d, v


def estr_for_pricing(day: date, archive_loader=None) -> tuple[date, float, str, str | None]:
    """€STR d'un pricing : (date, valeur %, source, avertissement).

    1. API de la BCE (avec nouveaux essais) ;
    2. si elle est indisponible : dernier €STR ≤ `day` du journal archivé par power-hpfc (data/ecb/estr.csv),
       signalé dans le résultat (date et ancienneté) ;
    3. sinon : erreur (aucun taux n'est supposé)."""
    try:
        d, v = estr(day)
        return d, v, "ECB API", None
    except LookupError as api_error:
        if archive_loader is None:
            raise
        try:
            journal = archive_loader()
        except LookupError as exc:
            raise LookupError(f"{api_error} ; journal €STR de power-hpfc indisponible : {exc}") from api_error
        journal = journal[[d <= day for d in journal.index]]
        if journal.empty:
            raise LookupError(f"{api_error} ; aucun €STR archivé avant le {day}") from api_error
        d, v = journal.index[-1], float(journal.iloc[-1])
        warning = (f"ECB API unavailable ({str(api_error).split(':')[0]}): €STR of {d} = {v:.3f} % taken from the "
                   f"power-hpfc archive ({(day - d).days} day(s) before the pricing date)")
        return d, v, "power-hpfc archive (ECB API unavailable)", warning


def offset(d: date, days: int) -> date:
    return d + timedelta(days=days)
