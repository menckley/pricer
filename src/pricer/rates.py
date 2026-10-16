"""Taux publiés par la BCE (API publique data-api.ecb.europa.eu) : €STR."""
from __future__ import annotations

import io
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests

ECB_URL = "https://data-api.ecb.europa.eu/service/data/"
ESTR_KEY = "EST/B.EU000A2X2A25.WT"                 # €STR, taux moyen pondéré par les volumes
MAX_ESTR_LAG_DAYS = 5


def _get_csv(path: str, params: dict) -> pd.DataFrame:
    r = requests.get(ECB_URL + path, params={**params, "format": "csvdata"}, timeout=30)
    if r.status_code == 404 or not r.text.strip():
        return pd.DataFrame()
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text))


def estr(day: date) -> tuple[date, float]:
    """Dernier €STR publié pour un jour ≤ `day` (en %). Erreur s'il date de plus de 5 jours."""
    df = _get_csv(ESTR_KEY, {"endPeriod": day.isoformat(), "lastNObservations": 1})
    if df.empty:
        raise LookupError(f"€STR introuvable à la date du {day}")
    row = df.sort_values("TIME_PERIOD").iloc[-1]
    d, v = date.fromisoformat(str(row["TIME_PERIOD"])), float(row["OBS_VALUE"])
    if (day - d).days > MAX_ESTR_LAG_DAYS or not np.isfinite(v):
        raise LookupError(f"dernier €STR publié le {d}, trop ancien pour un pricing au {day}")
    return d, v


def offset(d: date, days: int) -> date:
    return d + timedelta(days=days)
