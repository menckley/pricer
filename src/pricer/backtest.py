"""Backtest des pricings archivés : prix offert contre coût de couverture réalisé.

Pour chaque pricing du journal arrivé à maturité (dernier jour de livraison passé, spot réalisé complet), on
rejoue le profil horaire (reconstruit depuis la définition compacte archivée) :

    P&L = trésorerie client (prix offert × volume, reçue si « sell », payée si « buy »)
          + couverture EEX aux settlements archivés  + résidu (q − h) valorisé au spot day-ahead réalisé

avec q la position du produit (négative en vente), h la couverture équivalente (même signe que q) et r = q − h le
résidu (r < 0 acheté, r > 0 revendu en day-ahead), comme dans `hedging.py`. Le P&L est BRUT : frais EEX / day-ahead,
conversion fin→phys et financement ne sont pas rejoués. L'écart de coût réalisé par rapport au prix juste archivé
(`cost_error_eur_mwh` = coût réalisé − prix juste, > 0 : prix sous-évalué) mesure donc le biais de la méthode.

À partir de ces P&L par pricing, on publie biais et quantiles par groupe (produit, stratégie, méthode de déviation),
la VaR / CVaR empirique de la perte, et une marge proportionnelle à la CVaR consommée (RAROC). Rien n'est utilisé
par `quote` : ces mesures servent à choisir et calibrer, pas à modifier un prix.

Garde-fous : seuls les pricings livrés comptent ; les pricings de test sont exclus par un marqueur de libellé
(`TEST_PATTERN`, modifiable) ou par identifiant ; un groupe de moins de `MIN_PRICINGS` pricings n'est pas
concluant (`reliable` = False).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

MIN_PRICINGS = 20
QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)
TEST_PATTERN = r"(?i)test|zfvz|^opt\d*$"
TZ = "Europe/Paris"
PEAK_HOURS = (8, 20)
PEAK_WEEKDAYS = (0, 1, 2, 3, 4)
WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


class UnsupportedPricing(ValueError):
    """Pricing dont le profil horaire ne peut pas être reconstruit depuis le journal."""


def is_test(label: str, pattern: str = TEST_PATTERN, pricing_id: str = "", exclude_ids=()) -> bool:
    return pricing_id in set(exclude_ids) or re.search(pattern, label or "") is not None


def _hours(start: date, end: date, tz: str = TZ) -> pd.DatetimeIndex:
    loc = pd.date_range(pd.Timestamp(start, tz=tz), pd.Timestamp(end, tz=tz) + pd.Timedelta(days=1),
                        freq="h", inclusive="left")
    return loc.tz_convert("UTC")


def _peak(index: pd.DatetimeIndex, tz: str = TZ) -> np.ndarray:
    loc = index.tz_convert(tz)
    return np.isin(loc.weekday, PEAK_WEEKDAYS) & (loc.hour >= PEAK_HOURS[0]) & (loc.hour < PEAK_HOURS[1])


def profile_from_spec(spec: dict | None, tz: str = TZ) -> pd.Series:
    """Position horaire q (MW, négative en vente) d'un produit « block » ou « weekly » depuis sa définition."""
    kind = (spec or {}).get("type")
    if kind not in ("block", "weekly"):
        raise UnsupportedPricing(f"profil non reconstructible depuis le journal (type {kind!r})")
    idx = _hours(date.fromisoformat(spec["start"]), date.fromisoformat(spec["end"]), tz)
    sign = -1.0 if spec["side"] == "sell" else 1.0
    if kind == "block":
        masks = {"Base": np.ones(len(idx), bool), "Peak": _peak(idx, tz), "Offpeak": ~_peak(idx, tz)}
        if spec["profile"] not in masks:
            raise UnsupportedPricing(f"profil inconnu {spec['profile']!r}")
        on = masks[spec["profile"]]
    else:
        loc = idx.tz_convert(tz)
        rows = spec["grid_by_weekday_hours_0_23"]
        grid = np.array([[c == "1" for c in rows[d]] for d in WEEKDAYS])      # jour × heure
        on = grid[loc.weekday.to_numpy(), loc.hour.to_numpy()]
    return pd.Series(sign * float(spec["mw"]), index=idx[on])


def hedge_cash(hedge: list[dict], q: pd.Series, tz: str = TZ) -> pd.DataFrame:
    """Couverture h (MW) et trésorerie h × settlement (€) de chaque heure de q, depuis la table archivée."""
    h = pd.Series(0.0, index=q.index)
    cash = pd.Series(0.0, index=q.index)
    loc = q.index.tz_convert(tz)
    day = pd.Series(loc.date, index=q.index)
    peak = pd.Series(_peak(q.index, tz), index=q.index)
    for r in hedge:
        m = (day >= date.fromisoformat(str(r["start"])[:10])) & (day <= date.fromisoformat(str(r["end"])[:10]))
        if "Base MW" in r:
            for mw, settl, mask in ((r["Base MW"], r["Base settl."], m), (r["Peak MW"], r["Peak settl."], m & peak)):
                h[mask] += mw
                cash[mask] += mw * settl
        elif "MW" in r:                      # contrat coté couvert par lui-même : h = q sur ses heures
            h[m] += q[m]
            cash[m] += q[m] * r["settlement"]
        else:
            raise UnsupportedPricing("table de couverture archivée illisible")
    return pd.DataFrame({"h": h, "cash": cash})


def replay(record: dict, spot: pd.Series, tz: str = TZ) -> dict:
    """P&L brut d'un pricing archivé (dict du fichier JSON) rejoué sur le spot réalisé (série horaire UTC)."""
    q = profile_from_spec(record["product"].get("spec"), tz)
    missing = q.index.difference(spot.index)
    if len(missing) or spot.reindex(q.index).isna().any():
        raise LookupError(f"spot réalisé incomplet : {len(missing)} heures livrées absentes")
    hc = hedge_cash(record.get("hedge") or [], q, tz)
    resid = q - hc["h"]
    volume = float(q.abs().sum())
    client = -float(np.sign(q.iloc[0])) * record["offer_price"] * volume
    pnl = client + float(hc["cash"].sum()) + float((resid * spot.reindex(q.index)).sum())
    cost = record["offer_price"] - pnl / volume          # coût réalisé par MWh (hors frais et financement)
    return {"volume_mwh": volume, "pnl_eur": pnl, "pnl_eur_mwh": pnl / volume, "realized_cost_eur_mwh": cost,
            "cost_error_eur_mwh": cost - record["fair_price"]}


def load_records(root: str | Path = "pricings") -> list[dict]:
    """Fichiers JSON de pricings archivés sous `root` (hors journal.csv)."""
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(root).glob("*/*/*.json"))]


def _strategy(record: dict) -> str:
    return str(record.get("hedging") or record.get("context", {}).get("strategy selected") or "?")


def backtest(records: list[dict], spot: pd.Series, as_of: date, pattern: str = TEST_PATTERN, exclude_ids=(),
             tz: str = TZ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(résultats par pricing livré, pricings écartés avec la raison). `as_of` : seuls les pricings dont le
    dernier jour de livraison est strictement antérieur sont rejoués."""
    rows, skipped = [], []
    for rec in records:
        pid = rec.get("id", "?")
        if is_test(rec.get("label", ""), pattern, pid, exclude_ids):
            skipped.append({"id": pid, "reason": "pricing de test"})
            continue
        prod = rec.get("product") or {}
        if date.fromisoformat(prod["delivery_end"]) >= as_of:
            skipped.append({"id": pid, "reason": "livraison non terminée"})
            continue
        try:
            res = replay(rec, spot, tz)
        except (UnsupportedPricing, LookupError) as e:
            skipped.append({"id": pid, "reason": str(e)})
            continue
        dev = rec.get("deviation") or {}
        rows.append({"id": pid, "product_type": (prod.get("spec") or {}).get("type"), "strategy": _strategy(rec),
                     "deviation_method": dev.get("method"), "offer_price": rec["offer_price"],
                     "fair_price": rec["fair_price"], "margin_eur": rec["margin"]["eur"],
                     "risk_cost_eur": rec["risk"]["cost_eur"], "deviation_cost_eur": dev.get("cost_eur", 0.0),
                     **res})
    return pd.DataFrame(rows), pd.DataFrame(skipped, columns=["id", "reason"])


def var_cvar(pnl, confidence: float = 0.95) -> tuple[float, float]:
    """VaR et CVaR (expected shortfall) empiriques de la perte −P&L, en € (négatives en cas de gain).
    CVaR = moyenne des ⌈(1−α)·n⌉ pires pertes, au moins la pire."""
    if not 0.5 < confidence < 1:
        raise ValueError("niveau de confiance attendu dans ]0,5 ; 1[")
    loss = np.sort(-np.asarray(pnl, float))
    if loss.size == 0:
        raise ValueError("aucun P&L")
    var = float(np.quantile(loss, confidence))
    k = max(1, math.ceil(round((1 - confidence) * loss.size, 9)))
    return var, float(loss[-k:].mean())


@dataclass(frozen=True)
class Calibration:
    n: int
    reliable: bool
    var_eur: float
    cvar_eur: float
    margin_eur: float            # marge proportionnelle à la CVaR consommée
    raroc: float                 # marge archivée moyenne ÷ CVaR (NaN si CVaR ≤ 0)


def raroc_margin(cvar_eur: float, hurdle: float) -> float:
    """Marge cible = rendement exigé sur le capital × CVaR consommée (RAROC = marge ÷ CVaR), plancher 0."""
    if hurdle < 0:
        raise ValueError("rendement exigé négatif")
    return hurdle * max(0.0, cvar_eur)


def calibrate(results: pd.DataFrame, confidence: float = 0.95, hurdle: float = 0.15,
              min_n: int = MIN_PRICINGS) -> Calibration:
    n = len(results)
    if n == 0:
        raise ValueError("aucun pricing livré à calibrer")
    var, cvar = var_cvar(results["pnl_eur"], confidence)
    return Calibration(n, n >= min_n, var, cvar, raroc_margin(cvar, hurdle),
                       float(results["margin_eur"].mean() / cvar) if cvar > 0 else float("nan"))


def summarize(results: pd.DataFrame, by: str | list[str] = "strategy", min_n: int = MIN_PRICINGS) -> pd.DataFrame:
    """Biais (moyenne de `cost_error_eur_mwh`) et quantiles du P&L par groupe, avec indicateur de fiabilité."""
    cols = [by] if isinstance(by, str) else list(by)
    out = []
    for key, g in results.groupby(cols, dropna=False):
        key = key if isinstance(key, tuple) else (key,)
        row = dict(zip(cols, key))
        row.update({"n": len(g), "reliable": len(g) >= min_n, "bias_eur_mwh": float(g["cost_error_eur_mwh"].mean()),
                    "pnl_eur_mean": float(g["pnl_eur"].mean())})
        row.update({f"pnl_q{int(q * 100):02d}": float(g["pnl_eur"].quantile(q)) for q in QUANTILES})
        out.append(row)
    return pd.DataFrame(out)
