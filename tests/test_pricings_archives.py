"""Cohérence hors ligne des pricings archivés : schéma des JSON et correspondance 1-pour-1 avec le journal."""
import csv
import json
from datetime import datetime
from pathlib import Path

import pytest

from pricer.journal import COLUMNS

RACINE = Path(__file__).resolve().parent.parent
JOURNAL_CSV = RACINE / "pricings" / "journal.csv"
FICHIERS = sorted((RACINE / "pricings").glob("**/*.json"))

# Clés présentes dans tous les fichiers produits par journal.pricing_files (les autres sont optionnelles).
CLES_OBLIGATOIRES = {"id", "priced_at_utc", "label", "product", "fair_price", "offer_price", "price_build_up",
                     "hedging", "risk", "margin", "warnings", "curve", "pricer_version"}


def _journal() -> list[dict]:
    with JOURNAL_CSV.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _chemin(fichier: Path) -> str:
    return fichier.relative_to(RACINE).as_posix()


def test_journal_entete():
    with JOURNAL_CSV.open(newline="", encoding="utf-8") as f:
        assert next(csv.reader(f)) == COLUMNS


@pytest.mark.parametrize("fichier", FICHIERS, ids=_chemin)
def test_pricing_json_schema(fichier):
    d = json.loads(fichier.read_text(encoding="utf-8"))
    assert CLES_OBLIGATOIRES <= set(d), f"clés manquantes : {sorted(CLES_OBLIGATOIRES - set(d))}"
    assert fichier.stem == d["id"]
    assert fichier.parent.parts[-2:] == (f"{datetime.fromisoformat(d['priced_at_utc']):%Y}",
                                         f"{datetime.fromisoformat(d['priced_at_utc']):%m}")
    assert d["fair_price"] > 0 and d["offer_price"] > 0
    assert {"name", "side", "delivery_start", "delivery_end", "volume_mwh"} <= set(d["product"])


def test_journal_correspond_un_pour_un_aux_fichiers():
    lignes = _journal()
    ids = [r["id"] for r in lignes]
    dossiers = [r["folder"] for r in lignes]
    assert len(set(ids)) == len(ids), "identifiants dupliqués dans journal.csv"
    assert len(set(dossiers)) == len(dossiers), "chemins dupliqués dans journal.csv"
    assert set(dossiers) == {_chemin(f) for f in FICHIERS}, "journal.csv et pricings/**/*.json divergent"
    assert len(lignes) == len(FICHIERS) > 0


def test_journal_ligne_coherente_avec_son_fichier():
    for r in _journal():
        d = json.loads((RACINE / r["folder"]).read_text(encoding="utf-8"))
        assert d["id"] == r["id"] and d["label"] == r["label"] and d["pricer_version"] == r["pricer_version"]
        assert d["priced_at_utc"] == r["priced_at_utc"]
        assert float(r["offer_price"]) == pytest.approx(d["offer_price"], abs=1e-4)  # le journal arrondit à 4 décimales
