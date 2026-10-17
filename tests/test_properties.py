"""A hostname registered with nothing behind it is a finding, not a surprise.

The properties collector reads the platform's own property set out of the
checkout; the content rule turns a draft with no page into a medium finding
with a Gather button, and into a low one once a gather issue is open for it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from foreman.collectors.properties import PropertiesCollector, county_slug
from foreman.config import Project
from foreman.models import Observation
from foreman.rules import evaluate as evaluate_rules

PROPERTIES = {
    "operator": {"name": "ProCare Edge"},
    "properties": [
        {
            "hostname": "howardcountyhvac.com",
            "county": {"name": "Howard County", "state": "MD"},
            "trade": "hvac",
            "tracked_number": "+14105550134",
            "content_bundle": "howard-md-hvac",
            "status": "live",
        },
        {
            "hostname": "fairfaxcountyroofrepair.com",
            "county": {"name": "Fairfax County", "state": "VA"},
            "trade": "roofing",
            "content_bundle": "fairfax-va-roofing",
            "status": "draft",
            "aliases": ["fairfaxcountyroofer.com"],
        },
    ],
}


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "properties.json").write_text(json.dumps(PROPERTIES))
    (tmp_path / "content" / "howard-md-hvac").mkdir(parents=True)
    (tmp_path / "content" / "howard-md-hvac" / "page.md").write_text("# HVAC\n")
    return tmp_path


def _project(repo: Path | None) -> Project:
    return Project(id="p", repo=repo, github={"owner": "o", "repo": "r"})


def test_the_collector_reads_each_hostname_and_whether_it_has_a_page(tmp_path):
    rows = asyncio.run(PropertiesCollector().collect(_project(_repo(tmp_path))))
    facts = {}
    for r in rows:
        facts.setdefault(r.subject, {})[r.key] = r.value
    assert set(facts) == {"host:howardcountyhvac.com", "host:fairfaxcountyroofrepair.com"}
    howard = facts["host:howardcountyhvac.com"]
    assert howard["status"] == "live" and howard["has_page"] == "true"
    assert howard["has_number"] == "true" and "4105550134" not in str(howard)  # whether, not which
    fairfax = facts["host:fairfaxcountyroofrepair.com"]
    assert fairfax["status"] == "draft" and fairfax["has_page"] == "false"
    assert fairfax["county_slug"] == "fairfax-va" and fairfax["trade"] == "roofing"
    assert fairfax["aliases"] == "fairfaxcountyroofer.com"


def test_a_project_without_a_property_set_yields_nothing(tmp_path):
    assert asyncio.run(PropertiesCollector().collect(_project(tmp_path))) == []
    assert asyncio.run(PropertiesCollector().collect(_project(None))) == []


def test_county_slug_is_how_the_content_tree_names_it():
    assert county_slug("Howard County", "MD") == "howard-md"
    assert county_slug("Prince William County", "VA") == "prince-william-va"
    assert county_slug("Columbia", "MD") == "columbia-md"


def _rows(repo: Path, extra: list[Observation] = ()) -> list[dict]:
    rows = asyncio.run(PropertiesCollector().collect(_project(repo)))
    return [
        dict(project=o.project, collector=o.collector, subject=o.subject, key=o.key, value=o.value)
        for o in [*rows, *extra]
    ]


def test_a_draft_with_no_page_is_a_medium_finding_until_a_gather_is_open(tmp_path):
    repo = _repo(tmp_path)
    project = Project(id="p", repo=repo, github={"owner": "o", "repo": "r"}, domains=["content"])

    found = [f for f in evaluate_rules(project, _rows(repo)) if f.rule == "draft_without_content"]
    assert len(found) == 1
    (f,) = found
    assert f.severity == "medium" and f.subjects == ["host:fairfaxcountyroofrepair.com"]
    assert "registered and serving nothing" in f.summary
    assert "content/fairfax-va-roofing" in f.detail and "Fairfax County" in f.detail

    # The live Howard property has a page, so nothing is said about it.
    assert not any("howard" in f.summary for f in found)

    # Once an issue naming the host is open, the finding drops to low and
    # points at the issue: the board says "waiting on the gather".
    issue = Observation(
        project="p",
        collector="pulls",
        subject="issue:o/r#82",
        key="title",
        value="Gather what a Fairfax County homeowner needs to know about roof repair, "
        "for fairfaxcountyroofrepair.com",
    )
    found = [
        f
        for f in evaluate_rules(project, _rows(repo, [issue]))
        if f.rule == "draft_without_content"
    ]
    (f,) = found
    assert f.severity == "low" and "#82" in f.summary and "Issue #82" in f.detail
