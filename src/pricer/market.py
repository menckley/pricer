"""Données de marché d'une séance : HPFC horaire, settlements EEX, spot historique.

Toutes les données viennent du clone local de power-hpfc (archives du workflow quotidien).
Rien n'est approché : une donnée absente ou incohérente arrête le calcul.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import cached_property
from pathlib import Path

import numpy as np
import pandas as pd


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} a échoué dans {repo} :\n{r.stderr.strip()}")
    return r.stdout.strip()


def commit_info(repo: Path) -> dict:
    """Commit courant d'un dépôt (None s'il n'a aucun commit) et présence de modifications locales."""
    if not (Path(repo) / ".git").exists():
        return {"commit": None, "modifications_locales": None, "erreur": f"{repo} n'est pas un dépôt git"}
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "HEAD"],
                       capture_output=True, text=True)
    return {"commit": r.stdout.strip() or None, "modifications_locales": bool(git(repo, "status", "--porcelain"))}


def _pull(repo: Path) -> int:
    git(repo, "fetch", "--quiet")
    behind = int(git(repo, "rev-list", "--count", "HEAD..@{u}"))
    if behind:
        git(repo, "pull", "--ff-only", "--quiet")
    return behind


def sync_repo(repo: Path) -> int:
    """git pull --ff-only de power-hpfc et de son archive de données. data/ est un clone du dépôt privé
    power-hpfc-data (sinon, ancienne organisation : un dossier suivi par power-hpfc). Refuse si data/ a été modifié
    localement. Renvoie le nombre de commits récupérés."""
    data = Path(repo) / "data"
    nested = (data / ".git").exists()
    dirty = git(data, "status", "--porcelain") if nested else git(repo, "status", "--porcelain", "--", "data")
    if dirty:
        raise RuntimeError(f"des fichiers de data/ sont modifiés localement dans {repo} : "
                           "les annuler avant de synchroniser")
    behind = _pull(repo)
    if nested:
        behind += _pull(data)
    return behind


@dataclass(frozen=True, eq=False)
class Market:
    """Marché à une date de pricing. `curve` : index UTC horaire, colonnes price et is_peak."""
    name: str
    pricing_date: date
    tz: str
    unit: str
    tol: float                                   # tolérance de reproduction des settlements (EUR/MWh)
    peak_hours: tuple[int, int]                  # [début, fin[ en heure locale
    peak_weekdays: tuple[int, ...]               # 0 = lundi
    curve: pd.DataFrame
    settle: pd.DataFrame
    meta: dict = field(default_factory=dict)
    spot_loader: Callable[[date, date], pd.Series] | None = None
    hpfc_repo: Path | None = None
    options_loader: Callable[[], pd.DataFrame] | None = None        # archive des settlements d'options EEX
    history_loader: Callable[[], pd.DataFrame] | None = None        # archive de toutes les séances de futures
    estr_loader: Callable[[], pd.Series] | None = None              # journal des €STR (data/ecb/estr.csv)

    def __post_init__(self):
        c = self.curve
        if not {"price", "is_peak"} <= set(c.columns):
            raise ValueError("courbe : colonnes price et is_peak attendues")
        if not isinstance(c.index, pd.DatetimeIndex) or str(c.index.tz) != "UTC":
            raise ValueError("courbe : index horodaté en UTC attendu")
        if c["price"].isna().any() or not np.isfinite(c["price"].to_numpy(float)).all():
            raise ValueError("courbe : prix manquants")
        steps = c.index.to_series().diff().dropna().dt.total_seconds()
        if not (steps == 3600).all():
            raise ValueError("courbe : pas horaire continu attendu")
        need = {"profile", "kind", "name", "start", "end", "settlement"}
        if not need <= set(self.settle.columns):
            raise ValueError(f"settlements : colonnes attendues {sorted(need)}")

    # ------------------------------------------------------------------ grille
    @cached_property
    def local(self) -> pd.DatetimeIndex:
        return self.curve.index.tz_convert(self.tz)

    @cached_property
    def day(self) -> pd.Series:
        return pd.Series(self.local.date, index=self.curve.index)

    @cached_property
    def hour(self) -> pd.Series:
        return pd.Series(self.local.hour, index=self.curve.index)

    @cached_property
    def weekday(self) -> pd.Series:
        return pd.Series(self.local.weekday, index=self.curve.index)

    @cached_property
    def month(self) -> pd.Series:
        return pd.Series(self.local.strftime("%Y-%m"), index=self.curve.index)

    @property
    def price(self) -> pd.Series:
        return self.curve["price"]

    @property
    def is_peak(self) -> pd.Series:
        return self.curve["is_peak"].astype(bool)

    @property
    def first_day(self) -> date:
        return self.day.iloc[0]

    @property
    def last_day(self) -> date:
        return self.day.iloc[-1]

    def days(self, start: date, end: date) -> pd.Series:
        """Masque des heures livrées du jour start au jour end inclus ; erreur hors de la courbe."""
        if start > end:
            raise ValueError(f"début {start} après la fin {end}")
        if start < self.first_day or end > self.last_day:
            raise ValueError(f"période {start} → {end} hors de la courbe ({self.first_day} → {self.last_day}) : "
                             "pas de prix pour ces heures")
        return (self.day >= start) & (self.day <= end)

    def peak_mask(self, index: pd.DatetimeIndex) -> np.ndarray:
        """Heures peak (définition EEX du config power-hpfc) pour un index UTC quelconque."""
        loc = index.tz_convert(self.tz)
        a, b = self.peak_hours
        return np.isin(loc.weekday, self.peak_weekdays) & (loc.hour >= a) & (loc.hour < b)

    # ------------------------------------------------------------------ contrôles
    def check_settlements(self) -> pd.DataFrame:
        """Reprice chaque produit coté sur la courbe ; erreur si un écart dépasse la tolérance."""
        rows = []
        for r in self.settle.itertuples():
            m = (self.day >= r.start) & (self.day <= r.end)
            if r.profile == "Peak":
                m &= self.is_peak
            if not m.any():
                raise ValueError(f"settlement {r.profile} {r.name} hors de la courbe")
            rows.append({"profile": r.profile, "kind": r.kind, "name": r.name, "start": r.start, "end": r.end,
                         "settlement": r.settlement, "courbe": float(self.price[m].mean())})
        check = pd.DataFrame(rows)
        check["ecart"] = check["courbe"] - check["settlement"]
        worst = float(check["ecart"].abs().max())
        if worst > self.tol:
            raise ValueError(f"la courbe ne reproduit pas les settlements (écart max {worst:.4f} {self.unit})")
        return check

    def options(self) -> pd.DataFrame:
        """Settlements d'options EEX archivés (toutes séances), relus à chaque appel."""
        if self.options_loader is None:
            raise LookupError("aucune archive d'options EEX pour ce marché")
        return self.options_loader()

    def futures_history(self) -> pd.DataFrame:
        """Settlements de futures EEX archivés (toutes séances), relus à chaque appel."""
        if self.history_loader is None:
            raise LookupError("aucune archive de settlements de futures pour ce marché")
        return self.history_loader()

    def spot(self, start: date, end: date) -> pd.Series:
        if self.spot_loader is None:
            raise LookupError("aucune source de spot historique pour ce marché")
        return self.spot_loader(start, end)

    # ------------------------------------------------------------------ chargement
    @classmethod
    def load(cls, hpfc_repo: str | Path, market: str = "FR", pricing_date="latest", sync: bool = True) -> Market:
        """Charge la courbe et les settlements archivés par power-hpfc (clone local)."""
        repo = Path(hpfc_repo).expanduser()
        if not (repo / "pyproject.toml").exists():
            raise FileNotFoundError(f"dépôt power-hpfc introuvable : {repo}")
        behind = sync_repo(repo) if sync else None

        import hpfc
        from hpfc import archive
        from hpfc.config import load_config

        pkg_repo = Path(hpfc.__file__).resolve().parents[2]
        if not pkg_repo.samefile(repo):
            raise RuntimeError(f"le paquet hpfc importé vient de {pkg_repo}, pas de {repo} : "
                               "réinstaller avec `python -m pip install -e` depuis ce dépôt")
        data = repo / "data"
        cfg = load_config(market)
        available = archive.list_curves(market, data)
        if not available:
            raise FileNotFoundError(f"aucune courbe {market} archivée dans {data}")
        if pricing_date == "latest":
            d = available[-1]
        else:
            d = pricing_date if isinstance(pricing_date, date) else date.fromisoformat(str(pricing_date))
            if d not in available:
                near = [str(x) for x in available if abs((x - d).days) <= 7]
                raise FileNotFoundError(f"pas de courbe le {d} ; dates archivées proches : {near}")
        curve = archive.load_curve(market, d, data)[["price", "is_peak"]]
        meta = archive.load_curve_metadata(market, d, data)
        if meta["curve"]["steps"] != len(curve):
            raise ValueError("la courbe ne correspond pas à ses métadonnées")
        settle = archive.load_settlements(market, session=d, root=data)
        meta = {**meta, "_sync_commits": behind, "_available": [str(x) for x in available]}

        def spot_loader(start: date, end: date) -> pd.Series:
            return archive.load_spot(market, start, end, cfg.tz, hourly=True, root=data)

        def options_loader() -> pd.DataFrame:
            if not hasattr(archive, "load_options"):
                raise LookupError("power-hpfc trop ancien : archive des options EEX absente (mettre à jour le dépôt)")
            return archive.load_options(market, root=data)

        def history_loader() -> pd.DataFrame:
            return archive.load_settlements(market, root=data)

        def estr_loader() -> pd.Series:
            if not hasattr(archive, "load_estr"):
                raise LookupError("power-hpfc trop ancien : journal des €STR absent (mettre à jour le dépôt)")
            return archive.load_estr(root=data)

        mkt = cls(name=market, pricing_date=d, tz=cfg.tz, unit=cfg.unit, tol=cfg.arbitrage_tol,
                  peak_hours=(cfg.peak.start_hour, cfg.peak.end_hour), peak_weekdays=tuple(cfg.peak.weekdays),
                  curve=curve, settle=settle, meta=meta, spot_loader=spot_loader, hpfc_repo=repo,
                  options_loader=options_loader, history_loader=history_loader, estr_loader=estr_loader)
        mkt.check_settlements()
        return mkt

    def available_curves(self) -> list[date]:
        return [date.fromisoformat(x) for x in self.meta.get("_available", [])]

    def load_other_curve(self, d: date) -> pd.Series:
        """Prix d'une autre courbe archivée (backtests)."""
        if self.hpfc_repo is None:
            raise LookupError("pas de dépôt power-hpfc associé")
        from hpfc import archive
        return archive.load_curve(self.name, d, self.hpfc_repo / "data")["price"]

    def summary(self) -> str:
        return (f"Courbe {self.name} du {self.pricing_date} (méthode {self.meta.get('method', '?')}, "
                f"code {str(self.meta.get('code_version', '?'))[:7]}) : {len(self.curve)} heures, "
                f"livraison du {self.first_day} au {self.last_day} ; {len(self.settle)} produits EEX cotés")


def month_end(y: int, m: int) -> date:
    return date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
