"""Archive détaillée d'un pricing (exports/pricings/<date de pricing>/<libellé>/, ou zip dans l'application).
Le journal léger versionné sur GitHub est dans journal.py."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from pricer.market import Market, commit_info
from pricer.products import Product
from pricer.quote import Quote

REPO = Path(__file__).resolve().parents[2]
PRICINGS = REPO / "exports" / "pricings"        # archives détaillées locales (non versionnées)


def slug(label: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-").lower()
    if not s:
        raise ValueError("libellé vide")
    return s


def pricing_dir(mkt: Market, label: str, root: Path = PRICINGS) -> Path:
    return Path(root) / f"{mkt.pricing_date}" / slug(label)


def _clean(x):
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (float, np.floating)):
        return None if not np.isfinite(x) else float(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    return x


def save_quote(mkt: Market, q: Quote, label: str, root: Path = PRICINGS, overwrite: bool = False) -> Path:
    out = pricing_dir(mkt, label, root)
    if out.exists() and not overwrite:
        raise FileExistsError(f"pricing déjà archivé : {out}")
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"mw": q.product.mw}).rename_axis("utc").to_parquet(out / "position.parquet")
    q.lines.to_csv(out / "price_build_up.csv")
    q.hedge.table.to_csv(out / "hedge.csv")
    q.risk.by_month.to_csv(out / "risk_by_month.csv")
    q.risk.samples.to_csv(out / "risk_samples.csv", index=False)
    if len(q.financing):
        q.financing.to_csv(out / "funding.csv")
    if len(q.strategies):
        q.strategies.to_csv(out / "strategies.csv")
    if q.deviation is not None and len(q.deviation.by_month):
        q.deviation.by_month.to_csv(out / "deviation_by_month.csv")
    if q.indexation is not None:
        q.indexation["by_month"].to_csv(out / "index_by_month.csv")
    if q.deviation_model is not None:
        q.deviation_model.table(36).to_csv(out / "deviation_rmse_by_horizon.csv")
    record = {
        "label": label, "product": q.product.name, "physical": q.product.physical,
        "side": q.side,
        "archived_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "volume_mwh": q.volume, "fair_price": q.fair_price, "offer_price": q.price,
        "margin": {"eur": q.margin.eur, "rule": q.margin.rule, **q.margin.detail},
        "risk": {"cost_eur": q.risk.cost, "sigma_eur": q.risk.sigma},
        "deviation": None if q.deviation is None else {"cost_eur": q.deviation.cost, "sigma_eur": q.deviation.sigma,
                                                       "method": q.deviation.method},
        "indexation": None if q.indexation is None else {k: v for k, v in q.indexation.items() if k != "by_month"},
        "hedge": q.hedge_values, "context": q.context, "warnings": q.warnings,
        "curve": {"method": mkt.meta.get("method"), "code_version": mkt.meta.get("code_version"),
                  "hours": len(mkt.curve), "from": str(mkt.first_day), "to": str(mkt.last_day)},
        "power_hpfc": commit_info(mkt.hpfc_repo) if mkt.hpfc_repo else None,
        "pricer": commit_info(REPO) if (REPO / ".git").exists() else None,
    }
    (out / "pricing.json").write_text(json.dumps(_clean(record), indent=2, ensure_ascii=False, default=str),
                                      encoding="utf-8")
    return out


def load_position(path: Path) -> tuple[dict, Product]:
    record = json.loads((Path(path) / "pricing.json").read_text(encoding="utf-8"))
    pos = pd.read_parquet(Path(path) / "position.parquet")
    return record, Product(record["product"], pos["mw"], bool(record["physical"]))
