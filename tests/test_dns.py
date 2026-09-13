"""Setting a DNS record, and what it costs to have an effect on the internet.

Three things are being tested here and only the first is ordinary. The class has
to make eighteen domains pointed at one host into one decision, because that is
the entire reason a portfolio this size is workable. The before-state has to be
recorded in the action and re-checked twice, because a registrar has no
compare-and-set and nothing else remembers what was there. And the automation
policy has to grade on the TTL, because the thing a DNS mistake cannot take back
is not the record — it is every cache that answered while it was wrong.
"""

from __future__ import annotations

import pytest

import foreman.secrets
from foreman.actions import apply as apply_patch
from foreman.actions import build, policy_for, propose
from foreman.actions.base import OpNotApplicable
from foreman.actions.dns import MIN_TTL, SELF_HEALING_TTL, SetDnsRecord
from foreman.actions.sagform import automatable, policy_allows, precondition_holds
from foreman.collectors import godaddy_dns
from foreman.collectors.godaddy import GoDaddyError
from foreman.config import Project
from foreman.store import SqliteStore

HOST = "203.0.113.10"
OLD_HOST = "198.51.100.4"


@pytest.fixture
def registrar(monkeypatch):
    """A stand-in registrar: what is live now, and what was written to it.

    Nothing in this file reaches GoDaddy. The operator's domains are live, and a
    test that writes to them is not a test.
    """
    import foreman.actions.dns as dns_op

    world: dict = {"records": {}, "writes": [], "readable": True}

    def read(domain, record_type, name, token):
        assert token == "a-token"
        if not world["readable"]:
            raise GoDaddyError("the token was refused (403)")
        return [dict(row) for row in world["records"].get((domain, record_type, name), [])]

    def write(domain, record_type, name, data, ttl, token):
        assert token == "a-token"
        if not world["readable"]:
            raise GoDaddyError("the token was refused (403)")
        world["writes"].append((domain, record_type, name, data, ttl))
        world["records"][(domain, record_type, name)] = [{"data": data, "ttl": ttl}]

    monkeypatch.setattr(dns_op, "read_records", read)
    monkeypatch.setattr(godaddy_dns, "read_records", read)
    monkeypatch.setattr(godaddy_dns, "replace_records", write)
    monkeypatch.setattr(dns_op, "get_secret", lambda name: "a-token")
    monkeypatch.setattr(foreman.secrets, "get_secret", lambda name: "a-token")
    world["records"][("acme.test", "A", "@")] = [{"data": OLD_HOST, "ttl": 600}]
    return world


@pytest.fixture
def project():
    # No `repo`: a record is set at a registrar, not written into a working
    # tree, so this is the second op that acts on a project Foreman has never
    # checked out.
    return Project(id="p", registrar={"provider": "godaddy", "match": []})


def plan(project, **kw):
    settings = {
        "domain": "acme.test",
        "name": "@",
        "record_type": "A",
        "data": HOST,
        "ttl": 600,
    }
    settings.update(kw)
    (params,) = SetDnsRecord().plan(project, **settings)
    return params


def action(project, **kw):
    proposal = build(project, SetDnsRecord(), plan(project, **kw))
    assert proposal is not None
    return proposal


# --- the record set as text -------------------------------------------------


def test_a_record_set_reads_the_same_however_the_registrar_orders_it():
    """The API promises no order, and two reads differing only in one are not a
    change anybody made."""
    rows = [{"data": "1.2.3.4", "ttl": 600}, {"data": "5.6.7.8", "ttl": 600}]
    assert godaddy_dns.canonical(rows) == godaddy_dns.canonical(list(reversed(rows)))


def test_a_record_set_that_does_not_exist_reads_as_empty_the_way_a_new_file_does():
    assert godaddy_dns.canonical([]) == ""


def test_the_text_the_digest_hashes_is_the_text_the_registrar_was_read_through(registrar, project):
    """Two renderings of one record that could drift apart would make every
    action look permanently unapplied."""
    (record,) = action(project).patch.records
    assert record.after == godaddy_dns.canonical([{"data": HOST, "ttl": 600}])


# --- narrower than the credential -------------------------------------------


@pytest.mark.parametrize("record_type", ["NS", "SOA", "MX"])
def test_a_structural_record_is_refused_although_the_token_could_write_it(record_type, project):
    """The PAT grants domains.nameserver:update. Reading a nameserver is one
    thing; repointing delegation is a change that cannot be corrected through
    the zone it broke, and this operation's scope is narrower than the
    credential's on purpose."""
    with pytest.raises(OpNotApplicable):
        SetDnsRecord().plan(project, "acme.test", "@", record_type, "ns1.example.net", 600)


@pytest.mark.parametrize("record_type", ["NS", "SOA", "MX", "SRV", ""])
def test_the_write_path_refuses_the_same_types_on_its_own(record_type, monkeypatch):
    """Enforced twice. The op is where the decision is taken and this module is
    the only code that can reach the API, so a future caller that skipped the
    decision still gets stopped."""
    monkeypatch.setattr(godaddy_dns, "_call", lambda *a, **k: pytest.fail("reached the API"))
    with pytest.raises(GoDaddyError):
        godaddy_dns.replace_records("acme.test", record_type, "@", "x", 600, "a-token")
    with pytest.raises(GoDaddyError):
        godaddy_dns.read_records("acme.test", record_type, "@", "a-token")


def test_a_nameserver_refusal_says_why_rather_than_naming_a_list(project):
    with pytest.raises(OpNotApplicable, match="NS"):
        SetDnsRecord().plan(project, "acme.test", "@", "NS", "ns1.example.net", 600)
    with pytest.raises(GoDaddyError, match="every record at once"):
        godaddy_dns.replace_records("acme.test", "NS", "@", "ns1.example.net", 600, "a-token")


def test_a_ttl_the_registrar_would_reject_is_refused_before_it_is_sent(project):
    with pytest.raises(OpNotApplicable, match=str(MIN_TTL)):
        SetDnsRecord().plan(project, "acme.test", "@", "A", HOST, MIN_TTL - 1)


def test_a_domain_this_project_does_not_claim_is_refused(registrar):
    """One account holds every domain the operator owns and several projects
    divide them up. An action approved under one project must not reach
    another's domain."""
    narrow = Project(id="p", registrar={"provider": "godaddy", "match": ["other"]})
    with pytest.raises(OpNotApplicable, match="acme.test"):
        SetDnsRecord().plan(narrow, "acme.test", "@", "A", HOST, 600)


def test_a_project_with_no_registrar_account_is_refused(registrar):
    with pytest.raises(OpNotApplicable):
        SetDnsRecord().plan(Project(id="p"), "acme.test", "@", "A", HOST, 600)


# --- the equivalence class --------------------------------------------------


def test_the_same_record_on_eighteen_domains_is_one_decision(registrar, project):
    """The whole reason this scales. Pointing a portfolio at one host is one
    judgement applied many times, and a class per domain would be eighteen
    classes of one that never accumulate enough to mean anything."""
    keys = set()
    for index in range(18):
        domain = f"site{index}.test"
        registrar["records"][(domain, "A", "@")] = [{"data": OLD_HOST, "ttl": 600}]
        keys.add(action(project, domain=domain).class_key)
    assert len(keys) == 1


def test_pointing_at_a_different_host_is_a_different_decision(registrar, project):
    """The value is in the signature, unlike the package name in a bump.
    "Approve any patch bump" is a sentence an operator means; "approve an A
    record change, to anywhere" is consent to be repointed at an address nobody
    named."""
    assert action(project).class_key != action(project, data="192.0.2.7").class_key


def test_the_apex_and_a_subdomain_are_not_the_same_class(registrar, project):
    assert action(project).class_key != action(project, name="www").class_key


def test_a_wildcard_is_its_own_class(registrar, project):
    """It catches every name nobody declared, which is a blast radius of its own
    and not a subdomain's."""
    keys = {action(project, name=name).class_key for name in ("@", "*", "www")}
    assert len(keys) == 3


def test_two_hostnames_at_the_same_depth_are_one_class(registrar, project):
    """`www` and `vpn` are one decision about a subdomain. Signing on the
    literal name would file every hostname under a class of one."""
    assert action(project, name="www").class_key == action(project, name="vpn").class_key


def test_two_ttls_are_never_the_same_class(registrar, project):
    """The TTL decides how long a mistake outlives being noticed, which is what
    the automation policy grades on."""
    assert action(project).class_key != action(project, ttl=86400).class_key


def test_two_record_types_are_never_the_same_class(registrar, project):
    a = action(project)
    txt = action(project, record_type="TXT", data="v=spf1 -all")
    assert a.class_key != txt.class_key


def test_the_class_statement_names_nothing_that_varies_within_the_class(registrar, project):
    statement = action(project, name="www").class_statement
    for varying in ("acme.test", "www", OLD_HOST):
        assert varying not in statement


def test_a_text_record_survives_the_canonical_form(registrar, project):
    """DKIM values carry semicolons and quotes, and the class statement is SAG
    text that has to round-trip through a parser."""
    from foreman.actions.sagform import parse_action

    value = 'v=DKIM1; k=rsa; p="MIGfMA0G"'
    registrar["records"][("acme.test", "TXT", "mail._domainkey")] = []
    a = action(project, record_type="TXT", name="mail._domainkey", data=value)
    assert parse_action(a.statement).named_args["data"] == value
    assert parse_action(a.class_statement).named_args["data"] == value


# --- the digest -------------------------------------------------------------


def test_two_domains_are_two_proposals_even_when_the_change_reads_the_same(registrar, project):
    """The digest carries the domain, unlike a file edit's, which carries no
    project. One account holds every domain, so eighteen of them are eighteen
    targets inside one project — and the store keeps one open proposal per
    project, class and digest. Dropping the domain would collide seventeen of
    the eighteen and discard them as duplicates of the first."""
    registrar["records"][("other.test", "A", "@")] = [{"data": OLD_HOST, "ttl": 600}]
    assert action(project).patch_digest != action(project, domain="other.test").patch_digest


def test_every_domain_in_one_decision_reaches_the_pending_list(registrar, project):
    """The regression this guards is silent: the store returns None for a
    proposal it considers already pending, so seventeen dropped changes look
    exactly like a portfolio that only needed one."""
    made = []
    for index in range(18):
        domain = f"site{index}.test"
        registrar["records"][(domain, "A", "@")] = [{"data": OLD_HOST, "ttl": 600}]
        made.append(action(project, domain=domain))
    with SqliteStore(":memory:") as store:
        for a in made:
            assert (
                store.record_proposal(
                    project=a.project,
                    finding_id=None,
                    verb=a.verb,
                    statement=a.statement,
                    class_statement=a.class_statement,
                    class_key=a.class_key,
                    params=a.params,
                    patch_digest=a.patch_digest,
                    files=a.files,
                )
                is not None
            )
        pending = store.pending_actions()
    assert len(pending) == 18
    assert len({row["class_key"] for row in pending}) == 1


def test_a_domain_pointing_somewhere_else_to_begin_with_digests_differently(registrar, project):
    """Honest rather than convenient: the before-state is part of the change,
    so two domains starting from different places are not the same bytes."""
    registrar["records"][("other.test", "A", "@")] = [{"data": "192.0.2.99", "ttl": 600}]
    assert action(project).patch_digest != action(project, domain="other.test").patch_digest


def test_changing_the_value_changes_the_digest(registrar, project):
    assert action(project).patch_digest != action(project, data="192.0.2.7").patch_digest


def test_changing_the_ttl_changes_the_digest(registrar, project):
    assert action(project).patch_digest != action(project, ttl=1800).patch_digest


def test_the_same_operation_digests_the_same_every_time(registrar, project):
    assert action(project).patch_digest == action(project).patch_digest


def test_a_record_that_already_says_it_proposes_nothing(registrar, project):
    """No change is not the same as an action that does nothing. An empty patch
    never becomes a proposal."""
    registrar["records"][("acme.test", "A", "@")] = [{"data": HOST, "ttl": 600}]
    assert build(project, SetDnsRecord(), plan(project)) is None


# --- the before-state -------------------------------------------------------


def test_the_previous_value_is_recorded_in_the_action(registrar, project):
    """Once the PUT lands there is nowhere else it is written down. The action
    is the operator's copy of what was there."""
    a = action(project)
    assert a.params["before"] == f"{OLD_HOST} ttl=600"
    assert a.params["before"] in a.statement
    (record,) = a.patch.records
    assert record.before == f"{OLD_HOST} ttl=600"


def test_a_record_somebody_changed_since_it_was_read_fails_the_guardrail(registrar, project):
    """The only before-and-after check available on a surface with no
    compare-and-set, and what stops an approval granted this morning from
    overwriting a change made at lunchtime."""
    a = action(project)
    registrar["records"][("acme.test", "A", "@")] = [{"data": "192.0.2.55", "ttl": 600}]
    holds, why = precondition_holds(a.statement, SetDnsRecord().state(project, a.params))
    assert holds is False
    assert why


def test_an_unreadable_registrar_fails_the_guardrail_rather_than_looking_fixed(registrar, project):
    """A token that lapsed must not read as "the record already says that"."""
    a = action(project)
    registrar["readable"] = False
    holds, _ = precondition_holds(a.statement, SetDnsRecord().state(project, a.params))
    assert holds is False


def test_the_guardrail_reads_the_registrar_rather_than_the_parameters(registrar, project):
    """A precondition that restates what it was handed cannot fail, and one that
    cannot fail is decoration."""
    a = action(project)
    state = SetDnsRecord().state(project, a.params)
    assert state == {"dns": {"readable": True, "unchanged": True}}
    holds, _ = precondition_holds(a.statement, state)
    assert holds is True


def test_a_record_that_moved_has_to_be_proposed_again(registrar, project):
    a = action(project)
    registrar["records"][("acme.test", "A", "@")] = [{"data": "192.0.2.55", "ttl": 600}]
    with pytest.raises(OpNotApplicable, match="re-propose"):
        SetDnsRecord().render(project, a.params)


def test_a_registrar_that_cannot_be_read_produces_no_patch_at_all(registrar, project):
    """Computing a change against a record nobody could read is how a record set
    gets replaced by one that was guessed."""
    a = action(project)
    registrar["readable"] = False
    with pytest.raises(OpNotApplicable, match="could not be read"):
        SetDnsRecord().render(project, a.params)


# --- automation -------------------------------------------------------------


def test_a_short_lived_record_can_accumulate_towards_acting_unattended(registrar, project):
    """Pinned to one type, one scope, one value and one TTL, and refusing to act
    unless the record still says what it said when it was read. A class that
    narrow, corrected within the hour, is one an approval record can honestly
    speak for."""
    a = action(project, ttl=SELF_HEALING_TTL)
    assert automatable(a.statement) is True
    assert a.auto_eligible(approvals=10, rejections=0) is True
    assert a.auto_eligible(approvals=3, rejections=0) is False


def test_a_long_lived_record_never_earns_an_unattended_write(registrar, project):
    """Refusal by construction rather than a threshold somebody could raise. A
    day-long TTL is a mistake that outlives the working day it was made in, and
    what cannot be recalled is not the record but every cache that answered
    while it was wrong."""
    a = action(project, ttl=SELF_HEALING_TTL + 1)
    assert automatable(a.statement) is False
    for count in (0, 10, 500):
        assert policy_allows(a.statement, {"class": {"approvals": count, "rejections": 0}}) is False


def test_every_member_of_one_class_carries_the_same_policy(registrar, project):
    """The TTL is a signature field, so the policy cannot differ within a class —
    which is what makes "this class never acts unattended" a statement about the
    class rather than about one of its rows."""
    registrar["records"][("other.test", "A", "@")] = [{"data": OLD_HOST, "ttl": 3600}]
    slow = action(project, ttl=86400)
    also_slow = action(project, domain="other.test", ttl=86400)
    assert slow.class_key == also_slow.class_key
    assert automatable(slow.statement) == automatable(also_slow.statement) is False


def test_asked_without_parameters_the_op_still_answers(registrar):
    """A caller inspecting the registry has no class in hand. The flat answer
    stands there, and the graded one is only reached when there is a TTL to
    grade."""
    assert policy_for(SetDnsRecord())[0] == "auto"


def test_a_class_that_never_acts_unattended_still_keeps_its_record(registrar, project):
    """Refusing to automate is not refusing to count. "Approved forty times" is
    how an operator learns a class is boring, and it stays a fact."""
    a = action(project, ttl=86400)
    with SqliteStore(":memory:") as store:
        action_id = store.record_proposal(
            project=a.project,
            finding_id=None,
            verb=a.verb,
            statement=a.statement,
            class_statement=a.class_statement,
            class_key=a.class_key,
            params=a.params,
            patch_digest=a.patch_digest,
            files=a.files,
        )
        store.decide_action(action_id, "approved", "human")
        assert store.class_stats(a.class_key)["approvals"] == 1


# --- proposing --------------------------------------------------------------


def test_no_finding_proposes_a_record_change(registrar, project):
    """Nothing reads record values, so no finding says one points at the wrong
    place, and deriving the right value from a finding's prose is exactly what
    actions exist to not do. The parameters come from a plan instead."""
    rules = [
        "domain_will_not_renew",
        "domain_expiring",
        "domain_not_delegated",
        "domain_transfer_unlocked",
        "registrar_unreadable",
    ]
    for rule in rules:
        assert propose(project, [{"id": 1, "rule": rule, "subjects": ["domain:acme.test"]}]) == []


# --- applying ---------------------------------------------------------------


def test_applying_replaces_only_the_record_that_was_named(registrar, project):
    a = action(project)
    assert apply_patch(project, a) == [f"acme.test A → {HOST}"]
    assert registrar["writes"] == [("acme.test", "A", "@", HOST, 600)]


def test_a_subdomain_is_written_under_its_own_name(registrar, project):
    a = action(project, name="www", data="acme.test", record_type="CNAME")
    assert apply_patch(project, a) == ["www.acme.test CNAME → acme.test"]
    assert registrar["writes"] == [("acme.test", "CNAME", "www", "acme.test", 600)]


def test_a_record_that_moved_between_the_check_and_the_write_is_refused(registrar, project):
    """The read immediately before the write is as late as the check can be
    made. It cannot close the window — the API offers no compare-and-set — but
    it turns a change made an hour ago into a refusal rather than a silent
    overwrite."""
    a = action(project)
    registrar["records"][("acme.test", "A", "@")] = [{"data": "192.0.2.55", "ttl": 600}]
    with pytest.raises(OpNotApplicable, match="acme.test A"):
        apply_patch(project, a)
    assert registrar["writes"] == []


def test_an_unreadable_registrar_at_apply_time_writes_nothing(registrar, project):
    a = action(project)
    registrar["readable"] = False
    with pytest.raises(OpNotApplicable):
        apply_patch(project, a)
    assert registrar["writes"] == []


def test_nothing_is_written_while_the_proposal_is_only_being_computed(registrar, project):
    """Proposing is a read. The ledger, not the op, decides anything."""
    build(project, SetDnsRecord(), plan(project))
    assert registrar["writes"] == []


# --- the API call itself ----------------------------------------------------


def test_the_write_puts_one_record_to_the_type_and_name_it_names(monkeypatch):
    """PUT replaces the whole record set for a type and name, so sending one
    record is the only shape whose effect is legible from the action that
    proposed it."""
    calls = []

    def record(method, path, token, body=None):
        calls.append((method, path, body))

    monkeypatch.setattr(godaddy_dns, "_call", record)
    godaddy_dns.replace_records("acme.test", "a", "www", HOST, 600, "a-token")
    assert calls == [("PUT", "/domains/acme.test/records/A/www", [{"data": HOST, "ttl": 600}])]


def test_a_name_with_a_wildcard_in_it_is_escaped_into_the_path(monkeypatch):
    calls = []
    monkeypatch.setattr(godaddy_dns, "_call", lambda *a, **k: calls.append(a[1]) or [])
    godaddy_dns.read_records("acme.test", "A", "*", "a-token")
    assert calls == ["/domains/acme.test/records/A/%2A"]


# --- the dashboard ----------------------------------------------------------


def test_the_page_is_told_what_a_record_change_touches_and_what_it_costs(
    registrar, project, tmp_path
):
    """The Actions tab shows what each pending action will touch before anybody
    agrees to it. A record change has no file path, like a merge — and unlike a
    merge it does not land on a default branch, so the page is told the word and
    the sentence rather than inferring them from an empty file list."""
    import yaml
    from fastapi.testclient import TestClient

    from foreman.web import create_app

    registry_path = tmp_path / "foreman.yaml"
    registry_path.write_text(
        yaml.safe_dump({"projects": [{"id": "p", "registrar": {"provider": "godaddy"}}]})
    )
    a = action(project)
    db_path = tmp_path / "t.db"
    with SqliteStore(db_path) as store:
        store.record_proposal(
            project=a.project,
            finding_id=None,
            verb=a.verb,
            statement=a.statement,
            class_statement=a.class_statement,
            class_key=a.class_key,
            params=a.params,
            patch_digest=a.patch_digest,
            files=a.files,
        )

    (row,) = TestClient(create_app(registry_path, db_path)).get("/api/actions").json()
    assert row["verb"] == "set_dns_record"
    assert row["files"] == []
    assert row["target"] == f"acme.test A → {HOST}"
    assert row["effect"] == "set"
    assert "cached" in row["consequence"]
    assert row["automatable"] is True
    assert row["stale"] is None
    assert row["params"]["before"] == f"{OLD_HOST} ttl=600"


def test_a_merge_still_says_it_is_a_merge(registrar):
    """The word used to be inferred from an action having no files. Three kinds
    of effect broke that inference, and the op that relied on it has to keep
    reading the same way."""
    from foreman.actions import effect_of

    assert effect_of("bump_dependency") == (
        "merge",
        "This lands on the default branch and Foreman cannot undo it.",
    )
    assert effect_of("add_security_header")[0] == "apply"
    assert effect_of("add_security_header")[1] == ""
