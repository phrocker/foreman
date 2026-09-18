"""Plans, and the rule that keeps them honest.

A plan is the inverse of a finding: this does not exist yet and should. What
makes it Foreman rather than a task list is that a phase is done when a
collector says so. Nothing here may assert progress, so most of these tests are
about the four states — and particularly about the two that look alike and mean
opposite things.
"""

from __future__ import annotations

import json

from foreman.plans import (
    GATE_ONLY,
    GATES,
    Phase,
    Standing,
    pending_work,
    phase_op_params,
    plan_proposals,
    progress,
    standing,
    subjects_expecting,
    summarise,
)


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


def test_a_page_that_is_its_siblings_with_the_town_swapped_fails_substance():
    """The check that matters. Unique titles, unique descriptions and two
    thousand words apiece still describe one page with the place name changed —
    which is exactly what "the same product in eighteen locations" produces
    unless somebody stops it. The digest is taken with the domain's own name and
    every digit removed, so a template collapses onto its siblings."""
    templated = {"body_text_chars": "4000", "body_shared_with": "17"}
    assert not GATES["distinct"].check(templated, {})


def test_one_shared_body_is_allowed_because_two_names_for_one_site_is_ordinary():
    """`nbparkway.com` and `nationalbusinessparkway.com` are one business on two
    names, and failing that would be punishing a correct thing."""
    alias = {"body_text_chars": "4000", "body_shared_with": "1"}
    assert GATES["distinct"].check(alias, {})


def test_how_much_sharing_is_tolerated_is_the_operators_call():
    strict = {"body_text_chars": "4000", "body_shared_with": "1"}
    assert not GATES["distinct"].check(strict, {"max_shared": "0"})


def test_substance_reads_the_body_the_visitor_was_served():
    """It read `rendered_text_chars`, which only the render collector writes and
    only for a project's single web surface — so across eighteen domains it was
    never present and the gate could never be anything but unknown."""
    assert GATES["distinct"].needs == ("body_text_chars",)
    assert GATES["distinct"].check({"body_text_chars": "4000"}, {})
    assert not GATES["distinct"].check({"body_text_chars": "120"}, {})


def test_a_page_duplicating_its_siblings_has_not_passed_substance():
    """Eighteen local-services sites differing only by town and trade is the
    doorway pattern. This is the earliest signal that the set is thin — the same
    check a search console eventually complains about, run before it does."""
    thin = {"body_text_chars": "4000", "title_duplicated": "true"}
    assert not GATES["distinct"].check(thin, {})
    described = {"body_text_chars": "4000", "description_duplicated": "true"}
    assert not GATES["distinct"].check(described, {})


def test_substance_also_needs_enough_of_it():
    assert not GATES["distinct"].check({"body_text_chars": "50"}, {"min_chars": "2000"})


def test_a_phone_number_is_enough_to_pass_the_capture_gate():
    """Most of a local trade's work arrives by phone. Failing a site whose
    capture route is a number in the header would be this gate's worst
    available mistake."""
    assert GATES["captures"].check({"capture_route": "link"}, {})


def test_a_serving_page_nobody_can_get_in_touch_through_has_not_passed_capture():
    """A lead-generation site answering 200 with two thousand distinct words and
    no way to reach anybody has passed every earlier gate and delivered
    nothing."""
    assert not GATES["captures"].check({"capture_route": "none"}, {})


def test_an_operator_building_forms_can_refuse_to_count_a_phone_link():
    assert not GATES["captures"].check({"capture_route": "link"}, {"require": "form"})
    assert GATES["captures"].check({"capture_route": "form"}, {"require": "form"})


def test_a_domain_nobody_probed_for_capture_is_unknown_rather_than_failing():
    """The absence of evidence, and the fix is a sweep rather than any work."""
    assert GATES["captures"].needs == ("capture_route",)
    assert standing(_phase(gate="captures"), {}, True) is Standing.UNKNOWN


def test_the_capture_gate_never_needs_a_lead_to_be_submitted():
    """Proving capture works means writing to a live endpoint — a booking, a
    paged duty phone, a row somebody has to delete — which is a different kind
    of operation from everything else here and is deliberately not built. The
    gate reads a fact the probe observed without sending anything."""
    assert GATES["captures"].needs == ("capture_route",)
    assert "require" in GATE_ONLY


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


# --- a plan that acts -------------------------------------------------------


class _Surface:
    def claims(self, name):
        return True


class _Project:
    id = "domains"
    registrar = _Surface()
    active_domains = ("domains",)


class _Registry:
    active = (_Project(),)


class _Store:
    """Just enough store to drive the bridge."""

    def __init__(self, phases, subjects, facts, status="active"):
        self._phases = phases
        self._subjects = subjects
        self._facts = facts
        self._status = status

    def plans(self, status=None):
        if status and status != self._status:
            return []
        return [{"id": 1, "status": self._status}]

    def plan(self, plan_id):
        return {
            "id": 1,
            "status": self._status,
            "subjects": json.dumps(self._subjects),
            "goal": "g",
        }

    def phases(self, plan_id):
        return [
            {
                "id": i,
                "plan_id": 1,
                "position": p.position,
                "name": p.name,
                "gate": p.gate,
                "params": json.dumps(p.params),
            }
            for i, p in enumerate(self._phases, start=1)
        ]

    def project_summary(self):
        return [{"project": "domains"}]

    def latest_observations(self, project, as_of=None):
        return [
            {"subject": subject, "key": key, "value": value}
            for subject, facts in self._facts.items()
            for key, value in facts.items()
        ]


def test_only_the_subject_whose_turn_it_is_gets_work():
    """Proposing the content change for a domain whose DNS has not moved is
    work nobody can do, and a pending list nobody can act on is how people stop
    reading it."""
    phases = [_phase(1, nameserver_suffix="host.test"), _phase(2, gate="serves")]
    store = _Store(
        phases,
        ["domain:moved.test", "domain:parked.test"],
        {
            "domain:moved.test": {"nameservers": "ns1.host.test"},
            "domain:parked.test": {"nameservers": "ns1.domaincontrol.com"},
        },
    )
    work = pending_work(store, 1)
    # moved.test passed DNS and its Serve gate has no evidence, so it is not
    # pending — unknown is not a reason to act.
    assert [(s, p.name) for s, p in work] == [("domain:parked.test", "P1")]


def test_a_finished_plan_proposes_nothing():
    phases = [_phase(1, nameserver_suffix="host.test")]
    store = _Store(
        phases, ["domain:a.test"], {"domain:a.test": {"nameservers": "x"}}, status="done"
    )
    assert pending_work(store, 1) == []


def test_a_phase_with_no_operation_is_tracked_but_not_proposed():
    """Foreman is not the only thing that can do work, and a phase closed by a
    person is still worth watching."""
    phases = [_phase(1, nameserver_suffix="host.test")]
    store = _Store(phases, ["domain:a.test"], {"domain:a.test": {"nameservers": "ns1.other.test"}})
    assert pending_work(store, 1)
    assert plan_proposals(store, _Registry()) == []


def test_gate_parameters_are_not_handed_to_the_operation():
    """`nameserver_suffix` tells the gate what to look for and means nothing to
    the op; passing it on would be a confusing TypeError at the worst moment."""
    phase = _phase(1, nameserver_suffix="host.test", op="set_dns_record", data="203.0.113.1")
    assert "nameserver_suffix" not in phase_op_params(phase)
    assert phase_op_params(phase) == {"data": "203.0.113.1"}


def test_one_subject_failing_does_not_cost_the_others(monkeypatch):
    """A registrar that will not answer for one domain must not take the
    seventeen queued behind it down with it."""
    from foreman import plans as plans_module

    calls: list[str] = []

    class Flaky:
        verb = "set_dns_record"

        def plan(self, project, domain):
            calls.append(domain)
            if domain == "bad.test":
                raise RuntimeError("the registrar could not be read")
            return []

    monkeypatch.setattr(plans_module, "phase_op_params", lambda phase: {})
    import foreman.actions as actions_module

    monkeypatch.setattr(actions_module, "OPS", {"set_dns_record": Flaky()}, raising=False)

    phases = [_phase(1, nameserver_suffix="host.test", op="set_dns_record")]
    store = _Store(
        phases,
        ["domain:bad.test", "domain:good.test"],
        {
            "domain:bad.test": {"nameservers": "ns1.parked.test"},
            "domain:good.test": {"nameservers": "ns1.parked.test"},
        },
    )
    said: list[str] = []
    plan_proposals(store, _Registry(), log=said.append)
    assert calls == ["bad.test", "good.test"]
    assert any("registrar could not be read" in line for line in said)


# --- intent, read from the plan ---------------------------------------------


def _store(tmp_path):
    from foreman.store import SqliteStore

    return SqliteStore(tmp_path / "t.db")


def test_a_plan_declares_what_its_subjects_are_for(tmp_path):
    """The scoping fact the capture rule reads, and where it comes from.

    "No way to get in touch" is a real failure on a lead-generation site and
    nonsense on a job board, and no probe can tell them apart. The plan already
    says which is which — in the operator's own words, once — so it is read
    rather than declared a second time somewhere it could drift.
    """
    store = _store(tmp_path)
    with store as s:
        plan = s.create_plan("lead gen", ["domain:a.test", "domain:b.test"])
        s.add_phase(plan, 1, "Serve", "serves", {})
        s.add_phase(plan, 2, "Capture", "captures", {})
        other = s.create_plan("ship the job board", ["domain:jobs.test"])
        s.add_phase(other, 1, "Serve", "serves", {})

        assert subjects_expecting(s, "captures") == {"domain:a.test", "domain:b.test"}


def test_a_completed_plan_still_declares_its_subjects(tmp_path):
    """A plan that finished is a site that launched. A site that *stops*
    capturing after launch is the expensive version of this failure, so scoping
    on `active` would reintroduce the whole bug on a delay."""
    store = _store(tmp_path)
    with store as s:
        plan = s.create_plan("lead gen", ["domain:a.test"])
        s.add_phase(plan, 1, "Capture", "captures", {})
        s.set_plan_status(plan, "done")

        assert subjects_expecting(s, "captures") == {"domain:a.test"}
