import csv
import hashlib
import io
import json
import re
from datetime import date

import numpy as np
import pandas as pd
import pytest

from pricer import rates
from pricer.journal import (
    COLUMNS,
    COLUMNS_V2,
    JOURNAL,
    REPO_ENV,
    STATUTS,
    _csv_safe,
    _journal_with,
    archive_to_github,
    journal_repo,
    latest_status,
    pricing_files,
    status_event,
    update_status,
)
from pricer.products import grid_from_hours, weekly
from pricer.quote import quote
from tests.helpers import market, settings


class FakeGitHub:
    """Dépôt GitHub en mémoire : refs, commits, arbres, blobs (API Git Data + contents en raw)."""

    def __init__(self, race: int = 0):
        self.blobs, self.trees, self.commits = {}, {}, {}
        self.trees["t0"] = {"README.md": "b0"}
        self.blobs["b0"] = b"# pricings\n"
        self.commits["c0"] = {"tree": "t0", "parents": []}
        self.head, self.race = "c0", race

    class R:
        def __init__(self, code, payload=None, content=b""):
            self.status_code, self._p, self.content, self.text = code, payload, content, json.dumps(payload)

        def json(self):
            return self._p

    def files(self, commit=None):
        return {p: self.blobs[b] for p, b in self.trees[self.commits[commit or self.head]["tree"]].items()}

    def get(self, url, headers=None, params=None, timeout=None):
        path = re.sub(r"^.*/repos/[^/]+/[^/]+", "", url)
        if path == "/git/ref/heads/main":
            return self.R(200, {"object": {"sha": self.head}})
        if path.startswith("/git/commits/"):
            return self.R(200, {"tree": {"sha": self.commits[path.rsplit("/", 1)[1]]["tree"]}})
        if path in (f"/contents/{JOURNAL}", f"/contents/{STATUTS}"):
            name, f = path.removeprefix("/contents/"), self.files(params["ref"])
            return self.R(200, content=f[name]) if name in f else self.R(404, {})
        raise AssertionError(path)

    def post(self, url, headers=None, json=None, timeout=None):
        import base64
        path = re.sub(r"^.*/repos/[^/]+/[^/]+", "", url)
        if path == "/git/blobs":
            data = base64.b64decode(json["content"])
            sha = hashlib.sha1(data, usedforsecurity=False).hexdigest()
            self.blobs[sha] = data
            return self.R(201, {"sha": sha})
        if path == "/git/trees":
            t = dict(self.trees[json["base_tree"]])
            t.update({e["path"]: e["sha"] for e in json["tree"]})
            sha = f"t{len(self.trees)}"
            self.trees[sha] = t
            return self.R(201, {"sha": sha})
        if path == "/git/commits":
            sha = f"c{len(self.commits)}"
            self.commits[sha] = {"tree": json["tree"], "parents": json["parents"]}
            return self.R(201, {"sha": sha})
        raise AssertionError(path)

    def patch(self, url, headers=None, json=None, timeout=None):
        if self.race:                                   # quelqu'un d'autre a poussé entre-temps
            self.race -= 1
            self.commits["cx"] = {"tree": self.commits[self.head]["tree"], "parents": [self.head]}
            self.head = "cx"
            return self.R(422, {"message": "Update is not a fast forward"})
        if self.commits[json["sha"]]["parents"] != [self.head]:
            return self.R(422, {"message": "Update is not a fast forward"})
        self.head = json["sha"]
        return self.R(200, {"object": {"sha": json["sha"]}})


def _quote():
    m = market()
    p = weekly(m, date(2027, 1, 1), date(2027, 3, 31), 5, grid_from_hours(range(6, 22), range(5)), "sell")
    old, rates.estr = rates.estr, lambda d: (date(2026, 10, 12), 1.9)
    try:
        return m, quote(m, p, settings())
    finally:
        rates.estr = old


def test_each_pricing_is_one_commit_with_journal_line():
    m, q = _quote()
    gh = FakeGitHub()
    a1 = archive_to_github(m, q, "Client A 6-22", "o/r", "tok", http=gh)
    a2 = archive_to_github(m, q, "Client B", "o/r", "tok", http=gh)
    f = gh.files()
    lines = f[JOURNAL].decode().splitlines()
    assert lines[0].split(",") == COLUMNS and len(lines) == 3
    assert a1.folder in lines[1] and a2.folder in lines[2]
    assert a1.folder in f and a2.folder.endswith(".json") and len(f[a2.folder]) < 200_000
    rec = json.loads(f[a1.folder])
    assert rec["product"]["spec"]["type"] == "weekly" and rec["offer_price"] == pytest.approx(q.price)
    assert rec["environment"]["numpy"] == np.__version__ and rec["environment"]["pandas"] == pd.__version__
    assert "README.md" in f                                     # rien d'existant n'est supprimé
    assert gh.commits[a2.commit]["parents"] == [a1.commit]


def test_retry_when_branch_moved(monkeypatch):
    monkeypatch.setattr("pricer.journal.time.sleep", lambda s: None)
    m, q = _quote()
    gh = FakeGitHub(race=1)
    a = archive_to_github(m, q, "Client", "o/r", "tok", http=gh)
    assert gh.head == a.commit and gh.commits[a.commit]["parents"] == ["cx"]
    with pytest.raises(RuntimeError):
        archive_to_github(m, q, "Client", "o/r", "tok", http=FakeGitHub(race=5))


def test_conflits_repetes_puis_succes_avec_attente_exponentielle(monkeypatch):
    waits = []
    monkeypatch.setattr("pricer.journal.time.sleep", waits.append)
    m, q = _quote()
    gh = FakeGitHub(race=4)
    a = archive_to_github(m, q, "Client", "o/r", "tok", http=gh)
    assert gh.head == a.commit and waits == [0.5, 1.0, 2.0, 4.0]


def test_libelle_neutralise_dans_le_csv():
    m, q = _quote()
    _, row = pricing_files(m, q, "=HYPERLINK(\"x\")\nligne2")
    data = _journal_with(None, row).decode()
    rows = list(csv.reader(io.StringIO(data)))
    assert len(rows) == 2 and rows[1][2] == "'=HYPERLINK(\"x\") ligne2"
    assert row["label"].startswith("=")                                  # ligne d'origine non modifiée
    assert _csv_safe("Client A") == "Client A" and _csv_safe("@a") == "'@a"


def test_journal_row_content():
    m, q = _quote()
    files, row = pricing_files(m, q, "Test")
    assert set(row) == set(COLUMNS) and row["side"] == "sell" and row["offer_price"] > row["fair_price"]
    assert row["delivery_start"] == "2027-01-01" and row["delivery_end"] == "2027-03-31"
    assert list(files) == [row["folder"]] and row["folder"].startswith("pricings/")


def test_journal_repo_configurable_sans_defaut(monkeypatch):
    monkeypatch.delenv(REPO_ENV, raising=False)
    with pytest.raises(ValueError):
        journal_repo(None)
    with pytest.raises(ValueError):
        journal_repo("pasunrepo")
    monkeypatch.setenv(REPO_ENV, "o/prive")
    assert journal_repo(None) == "o/prive" and journal_repo("a/b") == "a/b"


def test_archive_sans_depot_configure_ne_pousse_rien(monkeypatch):
    monkeypatch.delenv(REPO_ENV, raising=False)
    m, q = _quote()
    with pytest.raises(ValueError):
        archive_to_github(m, q, "Client", None, "tok", http=FakeGitHub())


def test_ligne_v1_par_defaut_inchangee():
    m, q = _quote()
    _, row = pricing_files(m, q, "Client")
    assert set(row) == set(COLUMNS)
    assert _journal_with(None, row).decode().splitlines()[0].split(",") == COLUMNS


def test_ligne_v2_et_migration_du_journal_v1():
    m, q = _quote()
    _, old = pricing_files(m, q, "Ancien")
    v1 = _journal_with(None, old).decode()
    _, new = pricing_files(m, q, "Nouveau", client="ACME", is_test=False)
    assert new["status"] == "envoyé" and new["is_test"] == "false" and new["client"] == "ACME"
    rows = list(csv.DictReader(io.StringIO(_journal_with(v1, new).decode())))
    assert list(rows[0]) == COLUMNS_V2 and len(rows) == 2
    assert rows[0]["label"] == "Ancien" and rows[0]["status"] == "" and rows[0]["client"] == ""
    assert rows[1]["label"] == "Nouveau" and rows[1]["status"] == "envoyé"
    # journal v2 : une ligne v1 y est complétée par des cellules vides
    v2 = _journal_with(None, new).decode()
    rows = list(csv.DictReader(io.StringIO(_journal_with(v2, old).decode())))
    assert len(rows) == 2 and rows[1]["label"] == "Ancien" and rows[1]["is_test"] == ""


def test_entete_inconnue_refusee():
    m, q = _quote()
    _, row = pricing_files(m, q, "X", is_test=True)
    with pytest.raises(ValueError):
        _journal_with("a,b\n1,2\n", row)
    with pytest.raises(ValueError):
        _journal_with(",".join(COLUMNS_V2[:-1]) + "\n", row)


def test_status_event_validation():
    e = status_event("id1", "gagné", 101.5)
    assert e["status"] == "gagné" and e["won_price"] == 101.5
    assert status_event("id1", "perdu")["won_price"] == ""
    for args in (("id1", "inconnu"), ("id1", "perdu", 90.0), ("id1", "gagné", -1.0), ("id1", "gagné", float("nan"))):
        with pytest.raises(ValueError):
            status_event(*args)


def test_update_status_commit_complementaire_append_only():
    m, q = _quote()
    gh = FakeGitHub()
    a = archive_to_github(m, q, "Client", "o/r", "tok", http=gh, client="ACME", is_test=False)
    journal = gh.files()[JOURNAL]
    c1 = update_status(a.id, "perdu", "o/r", "tok", http=gh)
    c2 = update_status(a.id, "gagné", "o/r", "tok", won_price=99.0, note="=x", http=gh)
    f = gh.files()
    assert f[JOURNAL] == journal and gh.commits[c2]["parents"] == [c1]    # journal.csv intact
    events = list(csv.DictReader(io.StringIO(f[STATUTS].decode())))
    assert [e["status"] for e in events] == ["perdu", "gagné"] and events[1]["note"] == "'=x"
    assert latest_status(f[STATUTS].decode())[a.id]["status"] == "gagné"
    assert latest_status(None) == {}


def test_update_status_id_inconnu_ne_pousse_rien():
    m, q = _quote()
    gh = FakeGitHub()
    archive_to_github(m, q, "Client", "o/r", "tok", http=gh)
    head = gh.head
    with pytest.raises(ValueError):
        update_status("inconnu", "perdu", "o/r", "tok", http=gh)
    assert gh.head == head
