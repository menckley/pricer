"""Paramètres du pricer (config/pricer.yaml) et politique de marge (config/marge.yaml).

Ce sont des paramètres de l'utilisateur, pas des données de marché : un champ laissé vide (null) qui est
nécessaire au calcul arrête le pricing avec la liste des champs à renseigner. Aucune valeur n'est supposée.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _read(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}, hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class Settings:
    sourcing_days: int
    client_days: int
    day_count: int
    spread_bp: float | None
    eex_fee: float | None
    epex_fee: float | None
    conversion_fee: float | None
    liquidity_threshold_mw: float | None
    liquidity_eur_mwh_per_mw: float | None
    confidence: float
    window: int
    correlation: float
    history_start: date
    strategy: str
    margin_base: float | None
    margin_shape_pct: float | None
    margin_per_year: float | None
    margin_floor: float | None
    hash_pricer: str = ""
    hash_margin: str = ""

    @classmethod
    def load(cls, config_dir: str | Path = CONFIG_DIR) -> "Settings":
        d = Path(config_dir)
        p, h1 = _read(d / "pricer.yaml")
        m, h2 = _read(d / "marge.yaml")
        s = cls(sourcing_days=int(p["paiements"]["sourcing_jours"]), client_days=int(p["paiements"]["client_jours"]),
                day_count=int(p["paiements"]["base_jours"]), spread_bp=p["financement"]["spread_bp"],
                eex_fee=p["frais"]["eex_eur_mwh"], epex_fee=p["frais"]["epex_eur_mwh"],
                conversion_fee=p["frais"].get("conversion_fin_phys_eur_mwh"),
                liquidity_threshold_mw=(p.get("liquidite") or {}).get("seuil_mw"),
                liquidity_eur_mwh_per_mw=(p.get("liquidite") or {}).get("eur_mwh_par_mw"),
                confidence=float(p["risque"]["confiance"]), window=int(p["risque"]["fenetre_mois"]),
                correlation=float(p["risque"]["correlation_mois"]),
                history_start=p["risque"]["historique_debut"], strategy=p["couverture"]["strategie"],
                margin_base=m["base_eur_mwh"], margin_shape_pct=m["forme_pct"],
                margin_per_year=m["duree_eur_mwh_par_an"], margin_floor=m["plancher_eur"],
                hash_pricer=h1, hash_margin=h2)
        s.validate()
        return s

    def validate(self) -> None:
        errors = []
        if not 0 <= self.sourcing_days <= self.client_days:
            errors.append("paiements : 0 ≤ sourcing_jours ≤ client_jours attendu")
        if self.day_count not in (360, 365):
            errors.append("paiements : base_jours 360 ou 365")
        if not 0.5 < self.confidence < 1:
            errors.append("risque : confiance dans ]0,5 ; 1[")
        if not 0 <= self.correlation <= 1:
            errors.append("risque : correlation_mois dans [0 ; 1]")
        if self.window < 0 or self.window > 5:
            errors.append("risque : fenetre_mois entre 0 et 5")
        for name in ("eex_fee", "epex_fee", "conversion_fee", "liquidity_threshold_mw", "liquidity_eur_mwh_per_mw", "margin_base", "margin_per_year", "margin_floor", "margin_shape_pct"):
            v = getattr(self, name)
            if v is not None and float(v) < 0:
                errors.append(f"{name} négatif")
        from pricer.hedging import STRATEGIES
        if self.strategy != "auto" and self.strategy not in STRATEGIES:
            errors.append(f"couverture : strategie « auto » ou {', '.join(STRATEGIES)}")
        if errors:
            raise ValueError("paramètres invalides : " + " ; ".join(errors))

    def missing(self, need_financing: bool, manual_margin: bool, physical: bool = True) -> list[str]:
        out = []
        if physical and self.conversion_fee is None:
            out.append("config/pricer.yaml → frais.conversion_fin_phys_eur_mwh")
        if self.eex_fee is None:
            out.append("config/pricer.yaml → frais.eex_eur_mwh")
        if self.liquidity_threshold_mw is None:
            out.append("config/pricer.yaml → liquidite.seuil_mw")
        if self.liquidity_eur_mwh_per_mw is None:
            out.append("config/pricer.yaml → liquidite.eur_mwh_par_mw")
        if self.epex_fee is None:
            out.append("config/pricer.yaml → frais.epex_eur_mwh")
        if need_financing and self.spread_bp is None:
            out.append("config/pricer.yaml → financement.spread_bp")
        if not manual_margin:
            for key, v in (("base_eur_mwh", self.margin_base), ("forme_pct", self.margin_shape_pct),
                           ("duree_eur_mwh_par_an", self.margin_per_year), ("plancher_eur", self.margin_floor)):
                if v is None:
                    out.append(f"config/marge.yaml → {key}")
        return out
