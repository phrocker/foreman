"""Conversation about the portfolio.

Foreman knows a lot that its dashboard renders and nobody reads: which rules
earn their findings, what drifted, which classes have accumulated evidence.
Asking it a question is a better interface to that than four tabs.

Two constraints shape this, and both come from the rest of the system.

It never approves anything, and it never writes anything down as settled.
Actions are the operator's, and the whole ledger exists to make that decision
well-founded rather than to delegate it. The model may *suggest* an approval and
say why, and it may *propose* a memory when something durable has just been
decided; both arrive as buttons, and a human presses them. An assistant that
could approve its own suggestions would make the approval record measure its own
confidence instead of yours, and one that could promote its own guesses to
durable facts would be worse, because nothing later would mark them as guesses.

And every turn records what it was grounded in. An answer with no references is
an opinion, and the difference has to survive into storage — which is also what
turns these conversations into edges once the store is a graph.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, Field

from .actions import _ops, target_label
from .config import Registry
from .connectors import Connector, ConnectorError, Task, choose
from .graph import FROM_SKILL, SEEN_ON, key_of, node
from .memory import describe
from .plans import GATES
from .store import Store
from .work import open_pulls

TIMEOUT_S = 180

PROMPT = """You are Foreman, a portfolio monitor. Answer the operator's question
about the projects below.

Ground every claim in the state you are given. If something is not in it, say
so rather than inferring — "no data on that" is a useful answer and a guess is
not. Be brief; this is a chat panel, not a report.

You cannot approve or reject anything, and you cannot record anything as
settled. If an action should be taken, put it in `suggest` with a short reason
and the operator will decide.

That limit is about approval, not about what Foreman can do once approved. Each
project carries a `deliver` mode: `worktree` writes an approved change into the
local checkout, `pull_request` branches, commits and opens a pull request for
it. So "Foreman cannot open pull requests" is wrong — say which mode the project
is on, and that changing it is a one-line edit to the registry. A merge-effect
action such as `bump_dependency` is the exception: it merges a pull request
somebody else opened, so delivery does not apply to it at all.

## Portfolio state

{state}

## Conversation so far

{history}

## The operator asks

{question}

## Answer

`reply` is your answer, in markdown, a few sentences.

`refs` is what it rests on — the finding, action and project ids you actually
used. Leave a list empty rather than padding it.

`suggest` is usually empty. Offer an approval or rejection only when the state
plainly supports it, with one sentence saying why.

`remember` is how this conversation stops having to happen again. Record one
whenever the operator tells you something you could not have read from the state
above — a constraint, a decision, a preference, or the reason something is the
way it is. Those are exactly the facts that get explained again next week
because nothing wrote them down, so look for them rather than waiting for one to
be obvious.

Keep the bar, though. Not a summary of what was said, and not a restatement of a
finding: a finding is a problem, a memory is a judgement about how this
portfolio works. One sentence, still true next month. Name what it bears on in
`about` as `project|<id>`, `rule|<name>` or `finding|<id>`.

These are kept as you write them — there is no button and nobody confirms. That
raises the bar rather than lowering it: write only what you would still stand
behind next month, because a wrong one has to be retired by hand. Being wrong
costs a correction, not a loss; nothing here is ever deleted.

Approval is untouched by this. A memory says how things are around here. It can
say a rule is noise; it can never approve an action.

Only the rules listed under "Rules an operation can fix" have an operation
behind them. For any other finding there is nothing to queue, nothing to
approve, and no button anywhere that starts one — say that plainly and say what
fixing it would take by hand. Never send the operator to a control to make it
happen; if you are about to name a tab or a button, it must be one listed here.
Offering a way to act that does not exist is worse than saying you cannot,
because it sends somebody looking for it.

A pull request carrying unresolved comments can be handed to an agent: it works
in a throwaway copy of the repository, makes the changes the reviewer asked for,
and pushes a commit to that pull request's branch. It cannot reach the default
branch and it cannot merge. Say so when the operator asks about one that is
marked as revisable, and name the button — it is on the Work tab. Do not offer
it for a pull request with no unresolved comments; there would be nothing to
act on.

`plan` is for when the operator is describing something they want to *build*
rather than something that is wrong. Findings are about what exists; a plan is
about what does not exist yet and should. Offer one only when the conversation
has settled on an actual shape of work, never as a way of restating a wish.

Phases are ordered and each names a gate, which is how Foreman will know the
step is done. The gates available are:

{gates}

Use `confirmed` for anything Foreman cannot observe — creating a cloud project,
signing something, work in a console it has no credential for. Give it
`key: confirmed:<short_name>`. That step is then a box the operator ticks, and
Foreman will not pretend to have checked it.

Use a measured gate wherever one fits, and add `op` to a phase's params when
Foreman could propose the work: `set_dns_record` is the one that exists.

A plan is drafted, never started. It waits for the operator."""


class Suggestion(BaseModel):
    action_id: int
    decision: str
    why: str = ""


class Memory(BaseModel):
    """A durable judgement the model thinks was just established.

    A proposal, never a write. `about` carries node ids — `project|mfa`,
    `rule|missing_security_header` — which is how it lands in the graph related
    to the thing it bears on instead of in a list nobody can traverse.
    """

    statement: str
    about: list[str] = Field(default_factory=list)
    why: str = ""


class DraftPhase(BaseModel):
    name: str
    gate: str
    # Gate arguments, and the op's if Foreman is to propose the work.
    params: dict[str, str] = Field(default_factory=dict)


class DraftPlan(BaseModel):
    """A shape of work, offered for the operator to accept or discard.

    Drafted, never started. A plan is cheap to propose and expensive to abandon
    half-done — eighteen subjects through five phases is a lot of pending work
    to conjure out of a conversational "what if" — so this arrives as something
    to look at and activate, and does nothing until somebody does.
    """

    goal: str
    subjects: list[str] = Field(default_factory=list)
    phases: list[DraftPhase] = Field(default_factory=list)


class Reply(BaseModel):
    reply: str
    refs: dict[str, list[Any]] = Field(default_factory=dict)
    suggest: list[Suggestion] = Field(default_factory=list)
    remember: list[Memory] = Field(default_factory=list)
    plan: DraftPlan | None = None


class ChatError(RuntimeError):
    pass


def portfolio_state(store: Store, registry: Registry) -> str:
    """What the model is allowed to reason from.

    Deliberately assembled here rather than given as tools to go and fetch. At
    this size the whole state is a few thousand tokens, so handing it over costs
    one round trip and removes every question about what it actually looked at.
    """
    lines: list[str] = _standing(store)
    lines.append("### Projects")
    for project in registry.active:
        surfaces = ",".join(project.surface_names) or "none"
        lines.append(
            # `deliver` is here because leaving it out produced a confidently
            # wrong answer: asked to open pull requests for a project's
            # dependency work, Foreman replied "I can't open PRs myself — I only
            # read state and suggest". It can; `deliver: pull_request` is what
            # turns an approval into one. Unable and not-configured are different
            # answers, and only one of them has a fix the operator can act on.
            f"- {project.id} ({project.label}) surfaces={surfaces} "
            f"domains={','.join(project.active_domains) or 'none'} "
            f"fixable={'yes' if project.fixable else 'no'} "
            f"deliver={project.deliver}"
        )

    # Pull requests, because "can you address the comment on #106" was answered
    # with "no data on that PR" while the Work tab was showing it. The collector
    # and the dashboard had it; the only thing that could not see it was the
    # thing being asked. State assembled in one place has to include everything
    # collected, or each new collector silently makes the assistant more wrong.
    pulls = open_pulls(store, registry)
    if pulls:
        lines.append(f"\n### Open pull requests ({len(pulls)})")
        for row in pulls:
            gate = row["blocking"] or "nothing blocking"
            lines.append(
                f"- {row['slug']}#{row['number']} [{row['project']}] {row['title']} "
                f"— {gate}; branch {row['branch']} → {row['base']}; "
                f"checks {row['checks'] or 'unknown'}; "
                f"{row['review_comments']} unresolved comment(s); "
                f"{'opened by Foreman' if row['foreman'] else 'opened by ' + row['author']}"
                + ("; an agent can be sent to address the comments" if row["revisable"] else "")
            )

    # Which rules an operation can actually answer. Without this the model has
    # no way to tell "nobody has queued a fix yet" from "nothing in this tool
    # can fix that", and it guessed — twice — telling the operator to queue a
    # duplicate-meta-description fix from the Work tab, which lists pull
    # requests and has no such button. An invented affordance is worse than a
    # refusal: it sends somebody looking for a control that was never built.
    answerable = sorted({rule for op in _ops().values() for rule in getattr(op, "answers", ())})
    lines.append("\n### Rules an operation can fix")
    lines.append(
        ", ".join(answerable)
        + ". Every other rule has no operation behind it: a finding under one can "
        "be dismissed or fixed by hand, and there is nothing to queue or approve."
    )

    findings = store.open_findings()
    lines.append(f"\n### Open findings ({len(findings)})")
    for row in findings:
        subjects = json.loads(row["subjects"])
        where = f" [{len(subjects)} subjects]" if subjects else ""
        lines.append(
            f"- #{row['id']} {row['severity']} {row['project']} {row['rule']}: "
            f"{row['summary']}{where} (source={row['source']})"
        )

    actions = store.pending_actions()
    lines.append(f"\n### Pending actions ({len(actions)})")
    for row in actions:
        stats = store.class_stats(row["class_key"], row["patch_digest"])
        decided = stats["approvals"] + stats["rejections"]
        record = (
            "no prior decisions"
            if decided == 0
            else f"{stats['approvals']}/{decided} approved across "
            f"{stats['projects']} project(s), {stats['verified']} verified, "
            f"{stats['broke']} broke the build"
        )
        # What it touches, rather than which files. The context a model is given
        # should not say "files=" and then nothing for an action whose effect is
        # a merge — an empty field reads as an action that changes nothing.
        touches = target_label(row["verb"], json.loads(row["params"]), json.loads(row["files"]))
        lines.append(f"- #{row['id']} {row['project']} {row['verb']} on {touches} — {record}")

    precision = store.rule_precision()
    if precision:
        lines.append("\n### Rule precision (acted on vs dismissed)")
        for row in precision:
            acted, decided = int(row["acted"] or 0), int(row["decided"])
            lines.append(f"- {row['rule']}: {acted}/{decided} acted on")
    else:
        lines.append("\n### Rule precision\nNot yet measured — no findings decided.")

    lines.append(_repo_activity(store, registry))
    lines.append(_domains(store))
    lines.append(_graph(store, findings))
    return "\n".join(lines)


# A registrar account is an asset inventory, not just a source of findings:
# which names are held, which are doing something, which are parked. That is a
# question worth asking an assistant, and it cannot be answered from findings
# alone because a parked domain is not a problem.
PARKING_HOSTS = ("domaincontrol.com", "afternic.com")
DOMAIN_LIST_LIMIT = 160


def _repo_activity(store: Store, registry: Registry) -> str:
    """What each repository has been doing, as a shape rather than a changelog.

    A project can be worth watching without being worth fixing. A stewarded one
    produces hundreds of events a quarter and none of them are findings; the
    question asked of it is "how is it going", which counts answer and a list
    does not — and a list would cost more than the answer is worth on every
    single turn.
    """
    from .history import summarise

    lines = [
        summarise(store, project.id) for project in registry.active if project.github is not None
    ]
    kept = [line for line in lines if line]
    if not kept:
        return ""
    return "\n### Repository activity (retained locally; no API call answers these)\n" + "\n".join(
        kept
    )


def _domains(store: Store) -> str:
    rows: dict[str, dict[str, str | None]] = {}
    for project in {r["project"] for r in store.project_summary()}:
        for cell in store.latest_observations(project):
            subject = str(cell["subject"])
            if subject.startswith("domain:"):
                rows.setdefault(subject.removeprefix("domain:"), {})[str(cell["key"])] = cell[
                    "value"
                ]
    if not rows:
        return ""

    live = {name: facts for name, facts in rows.items() if facts.get("status") == "ACTIVE"}
    if not live:
        return f"\n### Domains\n{len(rows)} held, none active."

    def provider(facts: dict[str, str | None]) -> str:
        servers = (facts.get("nameservers") or "").split(",")
        if not servers or not servers[0]:
            return "(nothing answers)"
        return ".".join(servers[0].split(".")[-2:])

    grouped: dict[str, list[tuple[str, str]]] = {}
    for name, facts in sorted(live.items()):
        grouped.setdefault(provider(facts), []).append((name, (facts.get("expires") or "")[:10]))

    parked = sum(
        n for host, names in grouped.items() for n in [len(names)] if host in PARKING_HOSTS
    )
    lines = [
        f"\n### Domains ({len(rows)} held, {len(live)} active, {parked} parked at the registrar)",
        "Grouped by who answers for them in public DNS. A domain on the registrar's own",
        "nameservers is parked — held but not serving anything, which is a decision",
        "rather than a fault, and the pool to draw on when considering what to build.",
    ]
    shown = 0
    for host, names in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        note = " — parked" if host in PARKING_HOSTS else ""
        lines.append(f"\n**{host}** ({len(names)}){note}")
        for name, expires in names:
            if shown >= DOMAIN_LIST_LIMIT:
                lines.append(f"  - … and {len(live) - shown} more")
                return "\n".join(lines)
            lines.append(f"  - {name} (expires {expires})")
            shown += 1
    return "\n".join(lines)


def _standing(store: Store) -> list[str]:
    """What has been decided, ahead of what is merely true today.

    First in the state on purpose. Everything below it is current state whose
    answer expires; these do not, and a model that reads them last has already
    formed its answer from the part that was going to change anyway.
    """
    memories = describe(store, store.memories())
    if not memories:
        return []
    lines = [
        "### Standing judgements",
        "Durable decisions the operator recorded. They outrank current state: a",
        "rule called noise here is noise. They authorise nothing — approval is",
        "counted from the ledger, never from one of these.",
    ]
    for row in memories:
        about = f" [about {', '.join(row['about'])}]" if row["about"] else ""
        lines.append(f"- #{row['id']} {row['statement']}{about}")
    lines.append("")
    return lines


def _graph(store: Store, findings: Sequence[Any]) -> str:
    """What the graph relates, so Foreman can answer questions about itself.

    Asked "what's in the graph", it used to say there wasn't one — the state it
    got was a flat list and it answered honestly from that. The store *is* a
    graph and the relationships are the part worth knowing: which rules recur
    across the portfolio, and which of them came from judgement rather than a
    check that fires wherever it applies.
    """
    lines = [
        "\n### The graph",
        "State is stored as a graph: project -has_finding-> finding -from_rule-> rule,",
        "with rule -seen_on-> project back the other way, finding -found_by-> skill,",
        "and rule -from_skill-> skill. Durable memories hang off it too: memory",
        "-about-> project, rule or finding, with project -remembers-> memory back the",
        "other way, memory -formed_in-> conversation for provenance, and",
        "memory -supersedes-> memory when one retires another. Dispatched agents are",
        "given the same relationships.",
    ]
    recurring: list[tuple[str, list[str], bool]] = []
    for rule in sorted({row["rule"] for row in findings}):
        seen = [key_of(p) for p in store.neighbors([node("rule", rule)], [SEEN_ON])]
        if len(seen) > 1:
            judged = bool(store.neighbors([node("rule", rule)], [FROM_SKILL]))
            recurring.append((rule, sorted(seen), judged))

    if not recurring:
        lines.append("\nNo rule is open on more than one project.")
        return "\n".join(lines)

    lines.append("\nRules open on more than one project — portfolio problems, not chores:")
    for rule, seen, judged in sorted(recurring, key=lambda r: (-len(r[1]), r[0])):
        source = "agent judgement" if judged else "deterministic check"
        lines.append(f"- {rule} on {', '.join(seen)} ({source})")
    return "\n".join(lines)


def _history(store: Store, conversation_id: int, limit: int = 12) -> str:
    turns = store.conversation(conversation_id)[-limit:]
    if not turns:
        return "(nothing yet)"
    return "\n".join(f"{t['role']}: {t['content']}" for t in turns)


async def ask(
    store: Store,
    registry: Registry,
    question: str,
    conversation_id: int | None = None,
    model: str | None = None,
    connectors: list[Connector] | None = None,
    on_text: Callable[[str], None] | None = None,
) -> tuple[int, Reply, float]:
    """Put a question to Foreman. Returns (conversation id, reply, USD spent)."""
    if connectors is None:
        from .connectors.claudecode import ClaudeCodeConnector

        connectors = [ClaudeCodeConnector()]
    if conversation_id is None:
        conversation_id = store.start_conversation(question[:80])
    store.add_message(conversation_id, "user", question)

    task = Task(
        instructions=PROMPT.format(
            state=portfolio_state(store, registry),
            history=_history(store, conversation_id),
            question=question,
            gates="\n".join(f"- `{name}`: {gate.summary}" for name, gate in sorted(GATES.items())),
        ),
        schema=Reply,
        # Nothing. The whole portfolio state is assembled above and handed
        # over, so this asks for no repository, no web and no shell — which is
        # what makes the chat pane work on any backend, including one with no
        # tools at all.
        needs=frozenset(),
        timeout_s=TIMEOUT_S,
        model=model,
        # The answer is `reply`; stream that as it is written.
        stream_field="reply",
    )
    try:
        result = await choose(connectors, task).run(task, on_text=on_text)
    except ConnectorError as exc:
        raise ChatError(str(exc)) from exc
    reply, cost = result.value, result.cost_usd

    store.add_message(
        conversation_id,
        "assistant",
        reply.reply,
        refs={k: v for k, v in reply.refs.items() if v},
        cost_usd=cost,
    )
    _keep(store, conversation_id, reply)
    return conversation_id, reply, cost


def _keep(store: Store, conversation_id: int, reply: Reply) -> None:
    """Record what the conversation established, without being asked twice.

    A memory used to arrive as an offer with a button on it. The operator's
    objection to that is the right one: the thing worth keeping was already
    identified, by them, in the conversation that just happened — making them
    click again to confirm it is not a safeguard, it is a second chance to
    forget. What made the button feel necessary was the worry about writing
    something down wrongly, and retirement is the answer to that: a memory is
    never deleted and `retire_memory` keeps the retraction as the record, so
    being wrong here costs a correction rather than a loss.

    Still not approval. A memory says "this is how things are around here"; it
    can say a rule is noise and it can never approve an action, which is the
    line that matters and is unchanged.

    Recorded here rather than in the page so `foreman ask` keeps what it learns
    too — a judgement that survives only when the operator happened to be in a
    browser is not durable.
    """
    for memory in reply.remember:
        statement = (memory.statement or "").strip()
        if not statement:
            continue
        if any(
            (row.get("statement") or "").strip() == statement
            and row.get("retired_at") in (None, "None")
            for row in store.memories()
        ):
            # The same judgement reached twice in two conversations is one
            # judgement. A second row would double its weight everywhere it is
            # read and give the operator two things to retire.
            continue
        store.remember(statement, about=memory.about or (), conversation_id=conversation_id)
