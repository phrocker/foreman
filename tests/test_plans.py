"""Plans, and the rule that keeps them honest.

A plan is the inverse of a finding: this does not exist yet and should. What
makes it Foreman rather than a task list is that a phase is done when a
collector says so. Nothing here may assert progress, so most of these tests are
about the four states — and particularly about the two that look alike and mean
opposite things.
"""

from __future__ import annotations

from foreman.plans import GATES, Phase, Standing, progress, standing, summarise


def _phase(position=1, gate="dns_resolves", **params):
    return Phase(
        id=position, plan_id=1, position=position, name=f"P{position}", gate=gate, params=params
    )


# --- the four states --------------------------------------------------------


def test_a_gate_that_holds_is_passed():
    facts = {"nameservers": "ns1.host.test,ns2.host.test"}
    assert standing(_phase(nameserver_suffix="host.test"), facts, True) is Standing.PASSED


def test_a_gate_that_does_not_hold_is_pending():
    facts = {"nameservers": "ns1.domaincontrol.com"}
    assert standing(_phase(nameserver_suffix="host.test"), facts, True) is Standing.PENDING


def test_a_phase_whose_turn_has_not_come_is_blocked_not_pending():
    """ "Not started" and "started and failing" call for completely different
    reactions, and collapsing them is how a stalled buildout looks busy."""
    facts = {"nameservers": "ns1.host.test"}
    assert standing(_phase(), facts, False) is Standing.BLOCKED


def test_a_gate_with_no_evidence_is_unknown_not_failed():
    """The absence of evidence is not evidence of failure. The fix here is to
    run a sweep, not to do any work."""
    assert standing(_phase(gate="serves"), {}, True) is Standing.UNKNOWN


def test_an_unknown_gate_name_does_not_silently_pass():
    assert standing(_phase(gate="wishful_thinking"), {"x": "y"}, True) is Standing.UNKNOWN


# --- the gates themselves ---------------------------------------------------


def test_dns_answering_at_all_is_enough_when_no_target_is_named():
    assert GATES["dns_resolves"].check({"nameservers": "ns1.anything.test"}, {})


def test_a_domain_nothing_answers_for_has_not_passed_dns():
    assert not GATES["dns_resolves"].check({"nameservers": ""}, {})


def test_serving_needs_a_body_and_not_just_a_status():
    """A parked page and a live site both return 200, and the difference between
    them is the entire project."""
    live = {"https_apex_status": "200", "cert_covers_name": "true", "body_text_chars": "5000"}
    parked = {"https_apex_status": "200", "cert_covers_name": "true", "body_text_chars": "120"}
    assert GATES["serves"].check(live, {})
    assert not GATES["serves"].check(parked, {})


def test_serving_needs_a_certificate_that_is_actually_valid():
    """Both halves of valid. A certificate that failed to verify and one issued
    for somebody else land the visitor on the same full-page warning, and a
    phase nobody can reach past is not a phase that is done."""
    served = {"https_apex_status": "200", "body_text_chars": "5000"}
    assert not GATES["serves"].check({**served, "cert_covers_name": "false"}, {})
    assert not GATES["serves"].check(
        {**served, "cert_covers_name": "true", "cert_error": "certificate has expired"}, {}
    )


def test_a_page_duplicating_its_siblings_has_not_passed_substance():
    """Eighteen local-services sites differing only by town and trade is the
    doorway pattern. This is the earliest signal that the set is thin — the same
    check a search console eventually complains about, run before it does."""
    thin = {"rendered_text_chars": "4000", "title_duplicated": "true"}
    assert not GATES["distinct"].check(thin, {})
    described = {"rendered_text_chars": "4000", "description_duplicated": "true"}
    assert not GATES["distinct"].check(described, {})


def test_substance_also_needs_enough_of_it():
    assert not GATES["distinct"].check({"rendered_text_chars": "50"}, {"min_words": "600"})


def test_a_gate_reading_a_number_it_cannot_parse_does_not_pass():
    """Unparseable is not success. Guessing here would mark a phase done on the
    strength of a typo."""
    assert not GATES["serves"].check(
        {"https_apex_status": "200", "cert_covers_name": "true", "body_text_chars": "lots"}, {}
    )


# --- ordering ---------------------------------------------------------------


def test_a_subject_stops_at_its_first_unpassed_phase():
    """What ordering means, and why a plan over eighteen domains is legible:
    the answer to "where are we" is a column rather than eighteen
    conversations."""
    phases = [
        _phase(1, nameserver_suffix="host.test"),
        _phase(2, gate="serves"),
        _phase(3, gate="distinct"),
    ]
    facts = {"a": {"nameservers": "ns1.domaincontrol.com"}}
    (row,) = progress(phases, ["a"], lambda s: facts[s]).values()
    assert row == [Standing.PENDING, Standing.BLOCKED, Standing.BLOCKED]


def test_each_subject_travels_the_phases_independently():
    """Eighteen domains are eighteen journeys through the same shape. One whose
    DNS has not propagated must not hold up its siblings."""
    phases = [_phase(1, nameserver_suffix="host.test"), _phase(2, gate="serves")]
    facts = {
        "moved": {
            "nameservers": "ns1.host.test",
            "https_apex_status": "200",
            "cert_covers_name": "true",
            "body_text_chars": "5000",
        },
        "parked": {"nameservers": "ns1.domaincontrol.com"},
    }
    rows = progress(phases, ["moved", "parked"], lambda s: facts[s])
    assert rows["moved"] == [Standing.PASSED, Standing.PASSED]
    assert rows["parked"] == [Standing.PENDING, Standing.BLOCKED]


def test_phases_are_evaluated_in_position_order_not_insertion_order():
    phases = [_phase(2, gate="serves"), _phase(1, nameserver_suffix="host.test")]
    facts = {"a": {"nameservers": "ns1.host.test"}}
    (row,) = progress(phases, ["a"], lambda s: facts[s]).values()
    # DNS first and passing, then Serve unknown for want of evidence.
    assert row[0] is Standing.PASSED


def test_the_summary_counts_every_subject_in_every_phase():
    phases = [_phase(1, nameserver_suffix="host.test")]
    facts = {"a": {"nameservers": "ns1.host.test"}, "b": {"nameservers": "ns1.other.test"}}
    rows = progress(phases, ["a", "b"], lambda s: facts[s])
    (line,) = summarise(rows, phases)
    assert line["passed"] == 1 and line["pending"] == 1
    assert line["phase"] == "P1"
