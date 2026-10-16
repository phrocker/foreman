"""Facts with provenance live in the store; the repository's yaml is an export.

Import reads a county file in, verify re-fetches every cited source and
records what it saw, export writes the file back out — and a round trip
with nothing changed is an empty diff.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from foreman import facts as F
from foreman.config import Project
from foreman.rules import evaluate as evaluate_rules
from foreman.store import SqliteStore

YAML = """
county: Fairfax County
state: VA
slug: fairfax-va
gathered_on: "2026-09-22"
facts:
  - id: fees.schedule
    topic: fees
    statement: Fees are set by Appendix Q.
    source: {publisher: LDS, title: Fee schedule, url: https://county.test/fees.pdf}
    dated: "2026-07-01"
    gathered_on: "2026-09-22"
    expires: "2027-07-01"
    method: read_off_pdf
    source_sha: "old-fees-sha"
  - id: permits.portal
    topic: permits
    statement: Permits go through PLUS.
    source: {publisher: LDS, title: Permits, url: https://county.test/permits}
    gathered_on: "2026-09-22"
    expires: "2027-09-22"
    method: fetched
    source_sha: ""
  - id: codes.state
    topic: codes
    scope: state
    trades: [hvac, plumbing]
    statement: The 2021 code applies.
    source: {publisher: DHCD, title: Codes, url: https://county.test/gone}
    gathered_on: "2026-09-22"
    expires: "2026-01-01"
    method: fetched
    source_sha: "x"
"""


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        yield s


def _repo(tmp_path: Path) -> Path:
    county = tmp_path / "content" / "counties" / "fairfax-va"
    county.mkdir(parents=True)
    (county / "facts.yaml").write_text(YAML)
    return tmp_path


def test_the_fingerprint_moves_with_the_words_not_the_markup():
    a = F.fingerprint(b"<html><script>x=1</script><p>Fee  $70</p></html>", "text/html")
    b = F.fingerprint(b"<html><script>x=2</script><div><p>Fee $70</p></div></html>", "text/html")
    c = F.fingerprint(b"<html><p>Fee $75</p></html>", "text/html")
    assert a == b and a != c
    assert F.fingerprint(b"%PDF-1.4 abc", "application/pdf") != F.fingerprint(
        b"%PDF-1.4 abd", "application/pdf"
    )


def test_import_reads_the_file_into_the_store_and_a_reimport_keeps_what_was_learned(
    store, tmp_path
):
    repo = _repo(tmp_path)
    (first,) = F.import_repo(store, "p", repo)
    assert first == {"added": 3, "updated": 0, "county": "county:fairfax-va"}
    rows = {r["fid"]: r for r in store.facts(project="p")}
    assert (
        rows["codes.state"]["scope"] == "state" and rows["codes.state"]["trades"] == "hvac,plumbing"
    )
    assert rows["fees.schedule"]["source_sha"] == "old-fees-sha"

    store.update_fact(rows["permits.portal"]["id"], status="verified", source_sha="stamped")
    (again,) = F.import_repo(store, "p", repo)
    assert again["updated"] == 3 and again["added"] == 0
    rows = {r["fid"]: r for r in store.facts(project="p")}
    # Unchanged fact: verification survives; the file's empty sha does not erase the stamp.
    assert rows["permits.portal"]["status"] == "verified"
    assert rows["permits.portal"]["source_sha"] == "stamped"


def test_export_is_the_file_back_again_and_a_round_trip_is_stable(store, tmp_path):
    F.import_repo(store, "p", _repo(tmp_path))
    text = F.export_yaml(store, "p", "county:fairfax-va", county="Fairfax County", state="VA")
    out = tmp_path / "out" / "fairfax-va" / "facts.yaml"
    out.parent.mkdir(parents=True)
    out.write_text(text)
    slug, doc = F.load_yaml(out)
    assert slug == "fairfax-va" and [f["id"] for f in doc["facts"]] == [
        "codes.state",
        "fees.schedule",
        "permits.portal",
    ]
    assert doc["facts"][0]["trades"] == ["hvac", "plumbing"] and doc["facts"][0]["scope"] == "state"
    # Importing the export changes nothing, and exporting again is byte-identical
    # but for the export date, which is today both times.
    F.import_facts(store, "p", out)
    assert (
        F.export_yaml(store, "p", "county:fairfax-va", county="Fairfax County", state="VA") == text
    )


def test_verify_updates_each_fact_and_records_what_the_rules_need(store, tmp_path):
    F.import_repo(store, "p", _repo(tmp_path))
    registry = SimpleNamespace(active=[SimpleNamespace(id="p")])
    pages = {
        "https://county.test/fees.pdf": ("200", "new-fees-sha"),
        "https://county.test/permits": ("200", "permits-sha"),
        "https://county.test/gone": ("404", ""),
    }
    counts = F.verify(
        store, registry, fetch=lambda urls: {u: pages[u] for u in urls}, today="2026-10-16"
    )

    assert counts == {"facts": 3, "fetched": 3, "changed": 1, "unreachable": 0, "expired": 1}
    rows = {r["fid"]: r for r in store.facts(project="p")}
    assert rows["fees.schedule"]["status"] == "changed"
    assert rows["permits.portal"]["status"] == "verified"
    assert rows["permits.portal"]["source_sha"] == "permits-sha"  # first stamp
    assert rows["codes.state"]["status"] == "expired"  # expiry wins over the 404
    assert all(r["verified_on"] for r in rows.values())

    project = Project(id="p", github={"owner": "o", "repo": "r"})
    findings = {f.rule: f for f in evaluate_rules(project, store.latest_observations("p"))}
    assert "fees.schedule" in findings["county_fact_source_changed"].detail
    assert "codes.state (expired 2026-01-01)" in findings["county_fact_expired"].detail
    assert "codes.state (404" in findings["county_fact_source_unreachable"].detail


def test_status_of_each_outcome():
    row = {"expires": "2099-01-01", "source_sha": "a"}
    assert F.status_of(row, "200", "a", "2026-10-16") == "verified"
    assert F.status_of(row, "200", "b", "2026-10-16") == "changed"
    assert F.status_of(row, "404", "", "2026-10-16") == "unreachable"
    assert F.status_of(row, "403", "", "2026-10-16") == "unverifiable"
    assert (
        F.status_of({"expires": "2020-01-01", "source_sha": "a"}, "200", "a", "2026-10-16")
        == "expired"
    )
    assert F.status_of({"expires": "", "source_sha": ""}, "200", "z", "2026-10-16") == "verified"


def test_summary_counts_each_county_by_status(store, tmp_path):
    F.import_repo(store, "p", _repo(tmp_path))
    (s,) = F.summary(store, "p")
    assert s["subject"] == "county:fairfax-va" and s["total"] == 3
    assert s["by_status"] == {"unverified": 3}


def test_the_content_domain_has_no_collector_of_its_own():
    from foreman.domains import DOMAINS

    assert DOMAINS["content"].collectors == ()


# --- research in, research out ---------------------------------------------------


def test_the_brief_names_what_stands_so_a_gather_does_not_regather(store, tmp_path):
    F.import_repo(store, "p", _repo(tmp_path))
    text = F.brief(store, "p", "county:fairfax-va")
    assert text.startswith("## Already established for this county (3 facts)")
    assert "`fees.schedule` (fees): Fees are set by Appendix Q." in text
    assert "hvac,plumbing only" in text
    assert F.brief(store, "p", "county:nowhere") == ""


def test_claims_that_stood_up_become_facts_and_a_rerun_lands_on_the_same_rows(store):
    from foreman.research import SUPPORTED, UNSUPPORTED, Checked, Claim, Facet, Verdict

    facet = Facet("permits", "Permits", "what permits?")
    good = Claim(
        statement="A roofing permit is $70.",
        source_url="https://c.test/fees",
        source_title="Fees",
        as_of="2026-07-01",
    )
    bad = Claim(
        statement="Permits are free.", source_url="https://c.test/fees", source_title="Fees"
    )
    unsourced = Claim(statement="Everyone says so.")
    result = SimpleNamespace(
        stands=(
            Checked(facet, good, Verdict(claim=0, verdict=SUPPORTED)),
            Checked(facet, unsourced, Verdict(claim=2, verdict=SUPPORTED)),
        ),
        rejected=(Checked(facet, bad, Verdict(claim=1, verdict=UNSUPPORTED)),),
    )

    out = F.from_research(
        store, "p", "county:fairfax-va", result, trade="roofing", today="2026-10-16"
    )
    assert out["facts"] == 1
    (row,) = store.facts(project="p")
    assert row["fid"].startswith("permits.") and row["trades"] == "roofing"
    assert row["status"] == "unverified" and row["source_sha"] == ""
    assert row["gathered_on"] == "2026-10-16" and row["expires"] == "2027-10-16"
    assert row["dated"] == "2026-07-01"

    F.from_research(store, "p", "county:fairfax-va", result, trade="roofing", today="2026-10-17")
    assert len(store.facts(project="p")) == 1
