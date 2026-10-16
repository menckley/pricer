"""Journal des pricings sur GitHub, dans le dépôt du pricer : un commit par pricing, fichiers légers.

    pricings/journal.csv                     une ligne par pricing (append-only)
    pricings/AAAA/MM/<horodatage>_<libellé>.json   l'essentiel : définition compacte du produit (pas la série
                                             horaire), construction du prix, couverture, stratégies, risque,
                                             marge, avertissements, paramètres

Le détail complet (position heure par heure, échantillons de risque…) reste téléchargeable dans l'application.
Écriture par l'API Git Data de GitHub (blobs → arbre → commit → mise à jour de la branche). Si un autre
pricing a été archivé entre-temps, la mise à jour est refaite une fois ; rien n'est jamais écrasé ni supprimé.
"""
from __future__ import annotations

import base64
import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from pricer import __version__
from pricer.archive import _clean, slug
from pricer.market import Market
from pricer.quote import Quote

API = "https://api.github.com"
JOURNAL = "pricings/journal.csv"
COLUMNS = ["id", "priced_at_utc", "label", "product", "side", "settlement", "delivery_start", "delivery_end",
           "volume_mwh", "fair_price", "offer_price", "margin_eur", "margin_rule", "risk_cost_eur", "hedging",
           "pricing_date", "pricer_version", "folder"]          # « folder » : chemin du fichier JSON du pricing


@dataclass(frozen=True)
class Archived:
    id: str
    folder: str
    commit: str
    url: str


def _records(df) -> list:
    return [] if df is None or df.empty else json.loads(df.reset_index().to_json(orient="records", date_format="iso"))


def pricing_files(mkt: Market, q: Quote, label: str, now: datetime | None = None) -> tuple[dict[str, bytes], dict]:
    """Fichier léger du pricing (chemin dans le dépôt → contenu) et ligne du journal."""
    now = now or datetime.now(timezone.utc)
    pid = f"{now:%Y%m%dT%H%M%SZ}_{slug(label)[:60]}"
    path = f"pricings/{now:%Y/%m}/{pid}.json"
    days = mkt.day[q.product.mw.index]
    record = {
        "id": pid, "priced_at_utc": now.isoformat(timespec="seconds"), "label": label,
        "product": {"name": q.product.name, "spec": q.product.spec, "physical": q.product.physical,
                    "side": q.side, "delivery_start": str(days.min()), "delivery_end": str(days.max()),
                    "hours": int(len(q.product.mw)), "volume_mwh": q.volume},
        "fair_price": q.fair_price, "offer_price": q.price,
        "price_build_up": _records(q.lines),
        "hedging": q.context["strategy selected"], "hedge": _records(q.hedge.table),
        "strategies": _records(q.strategies),
        "risk": {"cost_eur": q.risk.cost, "sigma_eur": q.risk.sigma, "confidence": q.risk.confidence,
                 "by_month": _records(q.risk.by_month)},
        "deviation": None if q.deviation is None else {
            "cost_eur": q.deviation.cost, "sigma_eur": q.deviation.sigma, "method": q.deviation.method,
            "by_month": _records(q.deviation.by_month)},
        "margin": {"eur": q.margin.eur, "rule": q.margin.rule, **q.margin.detail},
        "warnings": q.warnings, "context": q.context,
        "curve": {"pricing_date": str(mkt.pricing_date), "method": mkt.meta.get("method"),
                  "code_version": mkt.meta.get("code_version")},
        "pricer_version": __version__,
    }
    data = json.dumps(_clean(record), indent=1, ensure_ascii=False, default=str).encode("utf-8")
    row = {"id": pid, "priced_at_utc": now.isoformat(timespec="seconds"), "label": label,
           "product": q.product.name, "side": q.side,
           "settlement": "physical" if q.product.physical else "financial swap",
           "delivery_start": str(days.min()), "delivery_end": str(days.max()),
           "volume_mwh": round(q.volume, 3), "fair_price": round(q.fair_price, 4), "offer_price": round(q.price, 4),
           "margin_eur": round(q.margin.eur, 2), "margin_rule": q.margin.rule, "risk_cost_eur": round(q.risk.cost, 2),
           "hedging": q.context["strategy selected"], "pricing_date": str(mkt.pricing_date),
           "pricer_version": __version__, "folder": path}
    return {path: data}, row


def _journal_with(existing: str | None, row: dict) -> bytes:
    out = io.StringIO()
    if existing:
        header = existing.splitlines()[0].split(",") if existing.strip() else []
        if header != COLUMNS:
            raise ValueError(f"{JOURNAL} existant avec des colonnes inattendues : {header}")
        out.write(existing if existing.endswith("\n") else existing + "\n")
        w = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    else:
        w = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
    w.writerow(row)
    return out.getvalue().encode("utf-8")


class GitHubArchive:
    def __init__(self, repo: str, token: str, branch: str = "main", http=None):
        self.repo, self.branch = repo, branch
        self.http = http or requests.Session()
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"}

    def _call(self, method: str, path: str, ok=(200, 201), **kw):
        r = getattr(self.http, method)(f"{API}/repos/{self.repo}{path}", headers=self.headers, timeout=30, **kw)
        if r.status_code not in ok:
            raise RuntimeError(f"GitHub {method.upper()} {path} : HTTP {r.status_code} — {r.text[:200]}")
        return r

    def _journal(self, ref: str) -> str | None:
        r = self.http.get(f"{API}/repos/{self.repo}/contents/{JOURNAL}", params={"ref": ref}, timeout=30,
                          headers={**self.headers, "Accept": "application/vnd.github.raw"})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise RuntimeError(f"GitHub GET /contents/{JOURNAL} : HTTP {r.status_code} — {r.text[:200]}")
        return r.content.decode("utf-8")

    def push(self, files: dict[str, bytes], row: dict, message: str) -> Archived:
        for attempt in range(2):
            r = self._call("get", f"/git/ref/heads/{self.branch}", ok=(200, 404, 409))
            if r.status_code != 200:
                raise RuntimeError(f"branche {self.branch} introuvable dans {self.repo} : créer le dépôt avec un "
                                   "README (Initialize this repository) pour qu'il ait un premier commit")
            head = r.json()["object"]["sha"]
            base_tree = self._call("get", f"/git/commits/{head}").json()["tree"]["sha"]
            content = {**files, JOURNAL: _journal_with(self._journal(head), row)}
            tree = []
            for path, data in content.items():
                blob = self._call("post", "/git/blobs",
                                  json={"content": base64.b64encode(data).decode(), "encoding": "base64"}).json()
                tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
            new_tree = self._call("post", "/git/trees", json={"base_tree": base_tree, "tree": tree}).json()["sha"]
            commit = self._call("post", "/git/commits",
                                json={"message": message, "tree": new_tree, "parents": [head]}).json()["sha"]
            r = self._call("patch", f"/git/refs/heads/{self.branch}", ok=(200, 422),
                           json={"sha": commit, "force": False})
            if r.status_code == 200:
                return Archived(row["id"], row["folder"], commit,
                                f"https://github.com/{self.repo}/blob/{self.branch}/{row['folder']}")
        raise RuntimeError("la branche a changé deux fois pendant l'archivage : relancer le pricing")


def archive_to_github(mkt: Market, q: Quote, label: str, repo: str, token: str, http=None) -> Archived:
    files, row = pricing_files(mkt, q, label)
    msg = f"Pricing {row['id']} : {q.product.name} — {q.price:,.2f} €/MWh"
    return GitHubArchive(repo, token, http=http).push(files, row, msg)
