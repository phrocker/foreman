from foreman.models import Severity
from foreman.rules import evaluate


def rows(*triples):
    return [{"subject": s, "key": k, "value": v} for s, k, v in triples]


def test_duplicate_titles_are_found():
    findings = evaluate(
        "x",
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
        "x",
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
        "x",
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
        "x",
        rows(
            ("a.com", "probe_paths_tried", "3"),
            ("a.com", "probe_served_200", "3"),
            ("a.com", "probe_served_homepage_shell", "0"),
        ),
    )
    assert [f.rule for f in findings if f.rule.startswith("soft_404")] == ["soft_404"]


def test_unanchored_asset_disallow_blocks_rendering():
    findings = evaluate(
        "x",
        rows(
            ("a.com", "robots_txt", "User-agent: *\nDisallow: /assets\nDisallow: /admin\n"),
        ),
    )
    blocked = [f for f in findings if f.rule == "robots_blocks_assets"]
    assert len(blocked) == 1
    assert blocked[0].severity is Severity.HIGH


def test_anchored_asset_disallow_is_fine():
    findings = evaluate(
        "x",
        rows(
            ("a.com", "robots_txt", "User-agent: *\nDisallow: /assets$\n"),
        ),
    )
    assert not [f for f in findings if f.rule == "robots_blocks_assets"]
