from foreman.config import Project
from foreman.models import Severity
from foreman.rules import evaluate

PROJECT = Project(id="x", web={"url": "https://x"})


def rows(*triples):
    return [{"subject": s, "key": k, "value": v} for s, k, v in triples]


def test_duplicate_titles_are_found():
    findings = evaluate(
        PROJECT,
        rows(
            ("https://a.com/one", "status", "200"),
            ("https://a.com/one", "title", "Same Thing"),
            ("https://a.com/two", "status", "200"),
            ("https://a.com/two", "title", "Same Thing"),
        ),
    )
    dupes = [f for f in findings if f.rule == "duplicate_title"]
    assert len(dupes) == 1
    assert len(dupes[0].subjects) == 2


def test_unique_titles_are_not_flagged():
    findings = evaluate(
        PROJECT,
        rows(
            ("https://a.com/one", "status", "200"),
            ("https://a.com/one", "title", "One"),
            ("https://a.com/two", "status", "200"),
            ("https://a.com/two", "title", "Two"),
        ),
    )
    assert not [f for f in findings if f.rule == "duplicate_title"]


def test_homepage_shell_on_missing_urls_is_high():
    findings = evaluate(
        PROJECT,
        rows(
            ("a.com", "probe_paths_tried", "3"),
            ("a.com", "probe_served_200", "3"),
            ("a.com", "probe_served_homepage_shell", "3"),
        ),
    )
    soft = [f for f in findings if f.rule == "soft_404_shell"]
    assert len(soft) == 1
    assert soft[0].severity is Severity.HIGH


def test_custom_404_page_is_only_medium():
    findings = evaluate(
        PROJECT,
        rows(
            ("a.com", "probe_paths_tried", "3"),
            ("a.com", "probe_served_200", "3"),
            ("a.com", "probe_served_homepage_shell", "0"),
        ),
    )
    assert [f.rule for f in findings if f.rule.startswith("soft_404")] == ["soft_404"]


def test_unanchored_asset_disallow_blocks_rendering():
    findings = evaluate(
        PROJECT,
        rows(
            ("a.com", "robots_txt", "User-agent: *\nDisallow: /assets\nDisallow: /admin\n"),
        ),
    )
    blocked = [f for f in findings if f.rule == "robots_blocks_assets"]
    assert len(blocked) == 1
    assert blocked[0].severity is Severity.HIGH


def test_anchored_asset_disallow_is_fine():
    findings = evaluate(
        PROJECT,
        rows(
            ("a.com", "robots_txt", "User-agent: *\nDisallow: /assets$\n"),
        ),
    )
    assert not [f for f in findings if f.rule == "robots_blocks_assets"]


def test_project_summary_does_not_multiply_by_run_count(tmp_path):
    """Regression: rolling findings up across a join on `runs` counted each
    finding once per run, so per-project totals grew every sweep."""
    from foreman.models import Finding, Severity
    from foreman.store import SqliteStore

    with SqliteStore(tmp_path / "t.db") as store:
        run_ids = []
        for _ in range(5):  # five sweeps of the same site
            run_id = store.start_run("s1", "crawl")
            store.finish_run(run_id, ok=True)
            run_ids.append(run_id)
        store.record_findings(
            run_ids[-1],
            [Finding(project="s1", rule="r", severity=Severity.HIGH, summary="one")],
        )
        row = store.project_summary()[0]
        assert (row["high"], row["medium"], row["low"]) == (1, 0, 0)


def test_served_and_rendered_title_disagreeing_is_high():
    """The SPA failure: users see one title, every non-JS crawler sees another."""
    findings = evaluate(
        PROJECT,
        rows(
            ("https://x/p", "status", "200"),
            ("https://x/p", "title", "Homepage Shell"),
            ("https://x/p", "rendered_title", "Actual Article Title"),
        ),
    )
    hit = [f for f in findings if f.rule == "title_only_after_js"]
    assert len(hit) == 1
    assert hit[0].severity is Severity.HIGH


def test_matching_titles_are_not_flagged():
    findings = evaluate(
        PROJECT,
        rows(
            ("https://x/p", "status", "200"),
            ("https://x/p", "title", "Same"),
            ("https://x/p", "rendered_title", "Same"),
        ),
    )
    assert not [f for f in findings if f.rule == "title_only_after_js"]


def test_client_side_only_content_is_flagged():
    findings = evaluate(
        PROJECT,
        rows(
            ("https://x/p", "status", "200"),
            ("https://x/p", "served_text_chars", "120"),
            ("https://x/p", "rendered_text_chars", "4200"),
        ),
    )
    assert [f.rule for f in findings if f.rule == "content_only_after_js"]


def test_prerendered_page_passes_the_text_ratio():
    findings = evaluate(
        PROJECT,
        rows(
            ("https://x/p", "status", "200"),
            ("https://x/p", "served_text_chars", "3900"),
            ("https://x/p", "rendered_text_chars", "4200"),
        ),
    )
    assert not [f for f in findings if f.rule == "content_only_after_js"]


def test_short_pages_are_not_ratio_tested():
    """A 200-char page has no meaningful ratio; don't manufacture a finding."""
    findings = evaluate(
        PROJECT,
        rows(
            ("https://x/p", "status", "200"),
            ("https://x/p", "served_text_chars", "10"),
            ("https://x/p", "rendered_text_chars", "200"),
        ),
    )
    assert not [f for f in findings if f.rule == "content_only_after_js"]


def test_redirect_is_not_mistaken_for_js_only_metadata():
    """Regression: the crawler does not follow redirects, so a 301 records no
    served title. Reading that absence as "JS-only" flagged every page behind a
    trailing-slash redirect."""
    findings = evaluate(
        PROJECT,
        rows(
            ("https://x/p", "status", "301"),
            ("https://x/p", "redirect_to", "https://x/p/"),
            ("https://x/p", "rendered_title", "Real Title"),
        ),
    )
    assert not [f for f in findings if f.rule == "title_only_after_js"]


def test_js_only_title_still_flagged_on_a_200():
    findings = evaluate(
        PROJECT,
        rows(
            ("https://x/p", "status", "200"),
            ("https://x/p", "rendered_title", "Real Title"),
        ),
    )
    assert [f.rule for f in findings if f.rule == "title_only_after_js"]


def test_a_project_only_runs_the_domains_it_opted_into():
    """A library with no web presence should not accrue SEO findings just
    because the observations happen to be readable by an SEO rule."""
    facts = rows(
        ("https://x/a", "status", "200"),
        ("https://x/a", "title", "Same"),
        ("https://x/b", "status", "200"),
        ("https://x/b", "title", "Same"),
        ("x", "cert_days_remaining", "3"),
    )
    security_only = Project(id="x", domains=["security"], web={"url": "https://x"})
    security_hit = {f.rule for f in evaluate(security_only, facts)}
    assert "cert_expiring" in security_hit
    assert not any(r.startswith(("duplicate_", "missing_")) for r in security_hit)

    seo_only = Project(id="x", domains=["seo"], web={"url": "https://x"})
    seo_hit = {f.rule for f in evaluate(seo_only, facts)}
    assert "duplicate_title" in seo_hit
    assert "cert_expiring" not in seo_hit


def test_an_unknown_domain_is_rejected_at_registry_load():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Project(id="x", domains=["astrology"])


# --- pull requests that are stuck ------------------------------------------


def _pull(add_days=10.0, **facts):
    from datetime import UTC, datetime, timedelta

    from foreman.rules import delivery as delivery_rules

    found = {}

    def add(rule, severity, summary, subjects, detail=None):
        found[rule] = (severity, summary)

    when = (datetime.now(UTC) - timedelta(days=add_days)).isoformat()
    delivery_rules.evaluate(
        {"pull:o/r#1": {"created_at": when, **facts}},
        add,
    )
    return found


def test_a_bot_bump_that_has_been_red_for_days_is_a_finding():
    """The case this was written for, and the quiet one: nobody is waiting on a
    dependency update, so it rots, and the advisory it would have closed stays
    open."""
    found = _pull(checks="failing", author="dependabot[bot]", checks_failing="2")
    assert found["pull_request_failing"][0].value == "medium"


def test_a_persons_branch_failing_is_news_to_nobody():
    """They opened it and they can see it. Same finding, quieter, so it does not
    crowd out the one nobody is watching."""
    found = _pull(checks="failing", author="phrocker")
    assert found["pull_request_failing"][0].value == "low"


def test_a_pull_request_that_went_red_this_morning_is_not_yet_a_problem():
    """A suite that just failed may be a flake and may already be being re-run.
    Filing it immediately is how a board fills with things that fix themselves."""
    assert _pull(add_days=0.5, checks="failing", author="dependabot[bot]") == {}


def test_a_draft_is_open_on_purpose():
    """Plenty of pull requests sit open deliberately, and filing those every
    night is how a board stops being read."""
    assert _pull(checks="failing", draft="true", author="dependabot[bot]") == {}


def test_a_pull_request_with_no_gate_against_it_is_left_alone():
    """Age is not a fault. Nothing is in this one's way."""
    assert _pull(add_days=90.0, checks="passing", author="phrocker") == {}


def test_foremans_own_request_left_to_rot_is_its_own_finding():
    """A tool that asks for a change and then lets its own pull request go stale
    is worse than one that never asked."""
    found = _pull(checks="passing", foreman="true", author="phrocker")
    assert "foreman_pull_request_undecided" in found


def test_a_reviewer_still_waiting_after_a_week_is_a_finding():
    """Review that goes unanswered is how people stop reviewing."""
    found = _pull(add_days=9.0, checks="passing", review_comments="3", author="phrocker")
    assert "3 unanswered review comment(s)" in found["review_unanswered"][1]


def test_a_comment_left_yesterday_is_not_nagging_material():
    assert _pull(add_days=2.0, checks="passing", review_comments="1", author="phrocker") == {}


def test_a_merged_pull_request_has_no_age_and_is_not_judged():
    """Retraction nulls a subject's cells but leaves the subject. Without the
    date there is nothing to judge, and guessing would file a finding against
    something that no longer exists."""
    from foreman.rules import delivery as delivery_rules

    found = {}
    delivery_rules.evaluate(
        {"pull:o/r#1": {"checks": None, "created_at": None}},
        lambda *a, **k: found.setdefault(a[0], a),
    )
    assert found == {}


def test_a_home_page_no_sitemap_lists_is_not_a_sitemap_finding() -> None:
    """The crawler reads every host's home page now, sitemap or no sitemap.

    A secondary host that redirects, or an app homepage that is deliberately
    noindex, is not a sitemap listing a page it never listed. Found by the
    adversarial review of the coverage change.
    """
    from foreman.rules import seo

    found: list[tuple] = []

    def add(rule, severity, summary, subjects=(), detail=None):
        found.append((rule, sorted(subjects)))

    pages = {
        # Deliberately noindex, and reached because it is a host's home page.
        "https://app.test/": {
            "status": "200",
            "meta_robots": "noindex",
            "discovered_via": "home",
        },
        # Reached the same way, and redirecting.
        "https://old.test/": {"redirect_to": "https://new.test/", "discovered_via": "home"},
        # This one a sitemap really did list.
        "https://www.test/gone": {
            "status": "200",
            "meta_robots": "noindex",
            "discovered_via": "sitemap",
        },
    }
    seo.evaluate(pages, add)

    rules = {r for r, _ in found}
    assert "sitemap_url_redirects" not in rules
    assert ("sitemapped_but_noindex", ["https://www.test/gone"]) in found, (
        "the page a sitemap did list must still be reported"
    )


def test_a_page_observed_before_provenance_existed_still_counts() -> None:
    """Every page observed before the shallow pass came from a sitemap.

    Reading a missing `discovered_via` as "not sitemapped" would take standing
    findings off the board for a sweep and put them back.
    """
    from foreman.rules import seo

    found: list[tuple] = []

    def add(rule, severity, summary, subjects=(), detail=None):
        found.append((rule, sorted(subjects)))

    seo.evaluate({"https://www.test/gone": {"redirect_to": "https://www.test/new"}}, add)

    assert ("sitemap_url_redirects", ["https://www.test/gone"]) in found


def test_a_page_the_crawler_could_not_place_is_not_called_sitemapped() -> None:
    """A missing provenance cell means the page predates the cell, and every
    page observed then came from a sitemap — so an absent cell keeps its old
    meaning and standing findings stay on the board. The case that would abuse
    that default is a page first seen on a sweep that could not read the
    sitemap, and the collector marks those "unknown" rather than leaving them
    absent.
    """
    from foreman.rules import seo

    found: list[tuple] = []

    def add(rule, severity, summary, subjects=(), detail=None):
        found.append((rule, sorted(subjects)))

    seo.evaluate(
        {
            # First seen on a sweep that could not read the sitemap.
            "https://blind.test/": {
                "status": "200",
                "meta_robots": "noindex",
                "discovered_via": "unknown",
            },
            # Observed before the cell existed, so it came from a sitemap.
            "https://legacy.test/gone": {"status": "200", "meta_robots": "noindex"},
        },
        add,
    )

    flagged = [subjects for rule, subjects in found if rule == "sitemapped_but_noindex"]
    assert flagged == [["https://legacy.test/gone"]]


def test_a_canonical_naming_the_bare_root_still_resolves() -> None:
    """The crawler records a site root as "<site>/" so one page is not two
    rows. A canonical naming the bare form then looked up nothing at all, and a
    page canonicalising to a root that redirects stopped being reported."""
    from foreman.rules import seo

    found: list[tuple] = []

    def add(rule, severity, summary, subjects=(), detail=None):
        found.append((rule, sorted(subjects)))

    seo.evaluate(
        {
            # Canonical written without the trailing slash.
            "https://x.test/page": {"status": "200", "canonical": "https://x.test"},
            # And the root, recorded the way the crawler records it.
            "https://x.test/": {"redirect_to": "https://www.x.test/"},
        },
        add,
    )

    assert ("canonical_to_redirect", ["https://x.test/page"]) in found


def test_a_root_canonicalising_to_itself_is_not_a_finding() -> None:
    """The two spellings are the same page, so a root whose canonical omits the
    slash is not canonicalising anywhere."""
    from foreman.rules import seo

    found: list[tuple] = []

    def add(rule, severity, summary, subjects=(), detail=None):
        found.append((rule, sorted(subjects)))

    seo.evaluate(
        {"https://x.test/": {"status": "200", "canonical": "https://x.test"}},
        add,
    )

    assert not [r for r, _ in found if r == "canonical_to_redirect"]
