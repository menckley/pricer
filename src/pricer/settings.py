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
    deviation_lookback: int | None = None          # jours de spot de la prévision par persistance
    deviation_day_window: int | None = None        # origines à ± n jours du jour du mois de la date de pricing
    deviation_min_months: int | None = None        # mois cibles distincts minimum par horizon
    deviation_excluded: tuple | None = None        # périodes exclues [(début, fin), ...]
    full_day_ahead: bool | None = None             # évaluer le scénario full day-ahead
    deviation_method: str | None = None            # persistance | implicite | historique (prime de niveau)
    risk_charging: str | None = None               # quantile | cout_du_capital
    cost_of_capital: float | None = None           # taux annuel du capital immobilisé (cout_du_capital)
    option_vol_source: str | None = None           # implicite | historique | prudente (prime des options)
    lot_mw: float | None = None                    # taille d'un lot EEX (1 contrat = 1 MW)
    odd_lot_eur_mwh: float | None = None           # prime par MWh EEX hors lot entier
    margin_index_base: float | None = None         # marge des contrats indexés spot (marge.yaml → indexation)
    margin_index_per_year: float | None = None
    margin_index_floor: float | None = None
    option_fee: float | None = None                # pricer.yaml → options : frais EEX par MWh sous-jacent et jambe
    option_vol_premium_pts: float | None = None    # points de volatilité ajoutés aux options vendues
    option_min_returns: int | None = None          # variations minimum pour la volatilité historique
    margin_option_base: float | None = None        # marge.yaml → options
    margin_option_floor: float | None = None
    hash_pricer: str = ""
    hash_margin: str = ""

    @classmethod
    def load(cls, config_dir: str | Path = CONFIG_DIR) -> "Settings":
        d = Path(config_dir)
        p, h1 = _read(d / "pricer.yaml")
        m, h2 = _read(d / "marge.yaml")
        dev = p.get("deviation") or {}
        mi = m.get("indexation") or {}
        po = p.get("options") or {}
        mo = m.get("options") or {}
        excl = dev.get("periodes_exclues")
        s = cls(sourcing_days=int(p["paiements"]["sourcing_jours"]), client_days=int(p["paiements"]["client_jours"]),
                day_count=int(p["paiements"]["base_jours"]), spread_bp=p["financement"]["spread_bp"],
                eex_fee=p["frais"]["eex_eur_mwh"], epex_fee=p["frais"]["epex_eur_mwh"],
                conversion_fee=p["frais"].get("conversion_fin_phys_eur_mwh"),
                liquidity_threshold_mw=(p.get("liquidite") or {}).get("seuil_mw"),
                liquidity_eur_mwh_per_mw=(p.get("liquidite") or {}).get("eur_mwh_par_mw"),
                lot_mw=(p.get("liquidite") or {}).get("lot_mw"),
                odd_lot_eur_mwh=(p.get("liquidite") or {}).get("prime_lot_incomplet_eur_mwh"),
                confidence=float(p["risque"]["confiance"]), window=int(p["risque"]["fenetre_mois"]),
                correlation=float(p["risque"]["correlation_mois"]),
                history_start=p["risque"]["historique_debut"], strategy=p["couverture"]["strategie"],
                margin_base=m["base_eur_mwh"], margin_shape_pct=m["forme_pct"],
                margin_per_year=m["duree_eur_mwh_par_an"], margin_floor=m["plancher_eur"],
                deviation_lookback=dev.get("fenetre_jours"), deviation_day_window=dev.get("fenetre_jour_du_mois"),
                deviation_min_months=dev.get("min_mois_cibles"),
                deviation_excluded=None if excl is None else tuple((a, b) for a, b in excl),
                full_day_ahead=dev.get("full_day_ahead"),
                deviation_method=dev.get("methode"), risk_charging=p["risque"].get("facturation"),
                cost_of_capital=p["risque"].get("taux_cout_capital"),
                option_vol_source=(p.get("options") or {}).get("volatilite_prime"),
                margin_index_base=mi.get("base_eur_mwh"), margin_index_per_year=mi.get("duree_eur_mwh_par_an"),
                margin_index_floor=mi.get("plancher_eur"),
                option_fee=po.get("frais_eur_mwh"), option_vol_premium_pts=po.get("prime_vol_points"),
                option_min_returns=po.get("min_variations_historique"),
                margin_option_base=mo.get("base_eur_mwh"), margin_option_floor=mo.get("plancher_eur"),
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
        for name in ("eex_fee", "epex_fee", "conversion_fee", "liquidity_threshold_mw", "liquidity_eur_mwh_per_mw", "margin_base", "margin_per_year", "margin_floor", "margin_shape_pct",
                     "margin_index_base", "margin_index_per_year", "margin_index_floor",
                     "option_fee", "option_vol_premium_pts", "margin_option_base", "margin_option_floor",
                     "odd_lot_eur_mwh"):
            v = getattr(self, name)
            if v is not None and float(v) < 0:
                errors.append(f"{name} négatif")
        from pricer.hedging import FULL_DA, STRATEGIES
        if self.strategy != "auto" and self.strategy not in STRATEGIES and self.strategy != FULL_DA:
            errors.append(f"couverture : strategie « auto », {', '.join(STRATEGIES)} ou {FULL_DA}")
        for a, b in self.deviation_excluded or ():
            if not (isinstance(a, date) and isinstance(b, date) and a <= b):
                errors.append("deviation : periodes_exclues = liste de [début, fin] (dates AAAA-MM-JJ)")
        if self.deviation_method is not None and self.deviation_method not in ("persistance", "implicite", "historique"):
            errors.append("deviation : methode « persistance », « implicite » ou « historique »")
        if self.risk_charging is not None and self.risk_charging not in ("quantile", "cout_du_capital"):
            errors.append("risque : facturation « quantile » ou « cout_du_capital »")
        if self.cost_of_capital is not None and not 0 <= self.cost_of_capital <= 1:
            errors.append("risque : taux_cout_capital entre 0 et 1 (ex. 0.10 pour 10 % par an)")
        if self.option_vol_source is not None and self.option_vol_source not in ("implicite", "historique", "prudente"):
            errors.append("options : volatilite_prime « implicite », « historique » ou « prudente »")
        if self.lot_mw is not None and not self.lot_mw > 0:
            errors.append("liquidite : lot_mw strictement positif (EEX : 1 MW)")
        if self.deviation_lookback is not None and self.deviation_lookback < 7:
            errors.append("deviation : fenetre_jours ≥ 7")
        if errors:
            raise ValueError("paramètres invalides : " + " ; ".join(errors))

    def missing_option(self, manual_margin: bool) -> list[str]:
        out = [f"config/pricer.yaml → options.{k}" for k, v in
               (("frais_eur_mwh", self.option_fee), ("prime_vol_points", self.option_vol_premium_pts),
                ("min_variations_historique", self.option_min_returns),
                ("volatilite_prime", self.option_vol_source)) if v is None]
        if not manual_margin:
            out += [f"config/marge.yaml → options.{k}" for k, v in
                    (("base_eur_mwh", self.margin_option_base), ("plancher_eur", self.margin_option_floor))
                    if v is None]
        return out

    def missing_indexed(self, need_financing: bool, manual_margin: bool) -> list[str]:
        """Paramètres nécessaires à un contrat indexé spot (pas de couverture EEX ni de risque à mesurer)."""
        out = []
        if self.epex_fee is None:
            out.append("config/pricer.yaml → frais.epex_eur_mwh")
        if self.conversion_fee is None:
            out.append("config/pricer.yaml → frais.conversion_fin_phys_eur_mwh")
        if need_financing and self.spread_bp is None:
            out.append("config/pricer.yaml → financement.spread_bp")
        if not manual_margin:
            for key, v in (("base_eur_mwh", self.margin_index_base), ("duree_eur_mwh_par_an", self.margin_index_per_year),
                           ("plancher_eur", self.margin_index_floor)):
                if v is None:
                    out.append(f"config/marge.yaml → indexation.{key}")
        return out

    def missing(self, need_financing: bool, manual_margin: bool, physical: bool = True,
                need_deviation: bool = False) -> list[str]:
        out = []
        if need_deviation:
            for key, v in (("fenetre_jours", self.deviation_lookback),
                           ("fenetre_jour_du_mois", self.deviation_day_window),
                           ("min_mois_cibles", self.deviation_min_months),
                           ("periodes_exclues", self.deviation_excluded), ("full_day_ahead", self.full_day_ahead),
                           ("methode", self.deviation_method)):
                if v is None:
                    out.append(f"config/pricer.yaml → deviation.{key}")
            if self.risk_charging is None:
                out.append("config/pricer.yaml → risque.facturation")
            elif self.risk_charging == "cout_du_capital" and self.cost_of_capital is None:
                out.append("config/pricer.yaml → risque.taux_cout_capital")
        if physical and self.conversion_fee is None:
            out.append("config/pricer.yaml → frais.conversion_fin_phys_eur_mwh")
        if self.eex_fee is None:
            out.append("config/pricer.yaml → frais.eex_eur_mwh")
        if self.liquidity_threshold_mw is None:
            out.append("config/pricer.yaml → liquidite.seuil_mw")
        if self.liquidity_eur_mwh_per_mw is None:
            out.append("config/pricer.yaml → liquidite.eur_mwh_par_mw")
        if self.lot_mw is None:
            out.append("config/pricer.yaml → liquidite.lot_mw")
        if self.odd_lot_eur_mwh is None:
            out.append("config/pricer.yaml → liquidite.prime_lot_incomplet_eur_mwh")
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
