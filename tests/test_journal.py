import hashlib
import re
import json
from datetime import date

import pytest

from pricer import rates
from pricer.journal import COLUMNS, JOURNAL, archive_to_github, pricing_files
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
        if path == f"/contents/{JOURNAL}":
            f = self.files(params["ref"])
            return self.R(200, content=f[JOURNAL]) if JOURNAL in f else self.R(404, {})
        raise AssertionError(path)

    def post(self, url, headers=None, json=None, timeout=None):
        import base64
        path = re.sub(r"^.*/repos/[^/]+/[^/]+", "", url)
        if path == "/git/blobs":
            data = base64.b64decode(json["content"])
            sha = hashlib.sha1(data).hexdigest()
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
    assert "README.md" in f                                     # rien d'existant n'est supprimé
    assert gh.commits[a2.commit]["parents"] == [a1.commit]


def test_retry_when_branch_moved():
    m, q = _quote()
    gh = FakeGitHub(race=1)
    a = archive_to_github(m, q, "Client", "o/r", "tok", http=gh)
    assert gh.head == a.commit and gh.commits[a.commit]["parents"] == ["cx"]
    with pytest.raises(RuntimeError):
        archive_to_github(m, q, "Client", "o/r", "tok", http=FakeGitHub(race=2))


def test_journal_row_content():
    m, q = _quote()
    files, row = pricing_files(m, q, "Test")
    assert set(row) == set(COLUMNS) and row["side"] == "sell" and row["offer_price"] > row["fair_price"]
    assert row["delivery_start"] == "2027-01-01" and row["delivery_end"] == "2027-03-31"
    assert list(files) == [row["folder"]] and row["folder"].startswith("pricings/")
