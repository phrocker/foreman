# Foreman

**Manage your projects.** Foreman keeps track of everything you run — 40-odd
projects, most with a website attached — observes them on a schedule, remembers
what it saw, and tells you the handful of things that actually need you today.

It runs locally, on one machine, as one operator. It is not a SaaS and should not
become one: the reason it can do things hosted tools cannot is that it has your
API keys and your source checkouts.

## What a project is

A project is the unit. A website is one *surface* of a project, alongside its
repository, its dependencies, its infrastructure and its costs. A project with no
`web:` block is still a project — the collectors that need a URL just skip it.

That distinction is load-bearing. SEO happened to be the first domain
implemented, and for a while this README described a portfolio SEO tool, because
a tool takes the permanent shape of whatever it was written for first.

```yaml
projects:
  - id: mfa
    name: MyFinanceAdvisor
    repo: /repos/MyFinanceAdvisor-tres     # → Foreman can open a PR
    domains: [seo, security, performance]
    web:
      url: https://www.myfinanceadvisor.com
      kind: spa

  - id: internal-lib
    name: A library with no website
    repo: /repos/internal-lib
    domains: [security]                     # no web surface; still a project
```

`repo` is the field that matters most. With a checkout, a finding can become a
branch and a pull request. Without one it can only ever become a line in a
report — which is where every hosted tool is permanently stuck.

## Design

**Collectors never call a model.** Everything under `collectors/` is a crawl, a
socket, or an API read: reproducible, free, identical across runs. The first four
real findings this produced were a robots.txt rule blocking every JS bundle on a
production site, 45 sitemap URLs answering redirects, `http://` serving 200
instead of redirecting, and a homepage shipping 5% of its text to non-JS
crawlers. All four were HTTP requests and string comparison. Nothing about
*finding* them needed intelligence.

**The model works on deltas, not corpora.** Once history exists, the nightly
question is "what changed and does it matter" — a few hundred tokens, not a
re-read of everything.

**One observation shape.** Collectors emit `(subject, key, value)` triples rather
than per-domain tables. A change is a row whose value differs from the previous
run's row with the same `(project, subject, key)`. Add a collector, get drift
detection free.

**Rules are partitioned by domain, projects opt in.** `rules/seo.py`,
`rules/security.py`, `rules/performance.py`. A project is evaluated on the
intersection of the surfaces it has and the domains it asked for, so a library
never accrues SEO findings. Adding a domain — costs, dependency freshness,
uptime — means a module here and a collector to feed it, and nothing else changes.

**Render only to compare.** The crawl collector reads what the server sends,
because that is what a non-JS crawler indexes. The optional `render` collector
reads what a browser produces after JavaScript runs. Where they disagree you have
found content that exists only for users. Plain Playwright with an honest
User-Agent — a stealth browser would defeat the purpose, since the question being
asked is "what does a crawler see".

**Probe for what the sitemap won't admit.** A sitemap only lists pages a site
claims to have, which makes it blind to unmatched URLs answering 200 with the
homepage shell. Foreman asks for URLs that should not exist and compares.

## Deliberately not built

- **No worker fleet, no queue, no provider router.** Collection across 40
  projects is minutes of wall clock on one machine. Distributing it would be
  infrastructure in search of a bottleneck. `budget.py` stays regardless —
  anything that spawns subagents needs a ceiling.
- **No stealth browser.** Foreman crawls projects you own. If a WAF blocks it,
  allowlist it.
- **No Postgres.** SQLite in WAL mode by default, behind a `Store` protocol,
  with a second implementation over shoal:

  ```bash
  shoal-embed serve --data ~/.shoal/foreman --port 9876
  FOREMAN_STORE=shoal://127.0.0.1:9876 foreman status
  ```

  Set it in `foreman.yaml` (`store: shoal://…`) so the choice travels with the
  projects it describes; `FOREMAN_STORE` overrides it for trying the other one.
  Point it at a server of its own — sharing one with another application means
  sharing a table.

  Both implementations are held to the same 42 parity tests, which drive an
  identical sequence through each and compare what comes back. `foreman migrate`
  moves between them and speaks only the protocol, so it runs in either
  direction — being able to copy back is what makes the move reversible. It
  refuses a destination that already holds findings, because it appends with
  fresh ids and a doubled ledger looks plausible.

  It carries current state, not version history: drift restarts after the next
  sweep. Copying every version of every cell is possible but the protocol does
  not expose it, and the current value is what rules read. Foreman's
  observation model turned out to be a cell store reinvented in SQL (project and
  subject are a row, the collector a column family, the key a column qualifier,
  `observed_at` a cell timestamp), so moving it onto one is a real prospect. The
  protocol and its tests are what keep that affordable.
- **No second model provider yet.** Worth adding for redundancy and cost
  arbitrage, not for speed — nothing here is rate-limited.

Each is one line of changed mind if the numbers move. None are true today.

## Quickstart

```bash
uv venv && uv pip install -e ".[dev]"
foreman init                 # writes foreman.yaml from the example
$EDITOR foreman.yaml
foreman collect              # observe every project, store a snapshot
foreman check                # run the deterministic rules
foreman status               # what needs attention, portfolio-wide
foreman serve                # dashboard at http://127.0.0.1:8765

# Browser rendering is an optional extra (Playwright + Chromium, ~150MB):
uv pip install -e ".[browser]" && playwright install chromium
foreman collect --collector render
```

## Commands

| Command | What it does |
| --- | --- |
| `foreman init` | Create `foreman.yaml` from the example |
| `foreman projects` | List projects and which are fixable |
| `foreman collect` | Observe, store a timestamped snapshot |
| `foreman check` | Evaluate the rules for each project's domains |
| `foreman status` | Open findings across the portfolio |
| `foreman audit [project]` | Escalate to a Claude Code skill |
| `foreman diff` | What changed since the previous snapshot |
| `foreman actions` | Pending actions, each with its class's record |
| `foreman approve` / `reject` | Decide one |
| `foreman apply-eligible` | Apply what a class has earned under its policy |
| `foreman ask "…"` | Ask about the portfolio; read-only |
| `foreman migrate shoal://…` | Copy this store into another one |
| `foreman verify` | Ask each project's checks whether applied actions held up |
| `foreman precision` | Which rules earn their findings |
| `foreman serve` | Dashboard on loopback (default `:8765`) |

`--project` scopes any of them; `--collector` scopes `collect`.

## Escalation: `foreman audit`

Collectors answer "what is true about this project". They cannot answer "is this
content any good", "is this dependency worth the risk", or "why does this rank
below a competitor" — those need judgement, and installed Claude Code skills
already have specialists. Foreman does not reimplement that. It decides *which*
of 40 projects deserve it and hands those few over.

```bash
foreman audit mfa --skill seo-page --budget 2.00
foreman audit --skill seo-geo --budget 20.00     # every project
```

Open deterministic findings go into the prompt so the agent skips what the sweep
already knows. Results come back as schema-validated JSON, namespaced
`skill/rule`, stored with `source='agent:<skill>'`. That last part matters twice:
the dashboard badges them, because a judgement call and a reproducible check
deserve different trust, and `foreman check` only deletes `source='rule'` rows,
so a nightly sweep cannot wipe results you paid for.

`--budget` is a real ceiling. Cost comes back in Claude Code's JSON envelope and
is recorded whether or not it fits — a ledger that discards an over-ceiling
charge reports `$0.00` against a real bill and gates nothing.

## Actions, and the evidence for trusting them

Actions are yours to approve. The ledger exists so that, eventually, some of
them need not be — and so that the case for automating one is arithmetic rather
than a feeling.

An action is not a patch. It is an instance of a registered operation, written
in [SAG](https://github.com/phrocker/sag):

```
DO anchor_asset_disallow(file="public/robots.txt",prefix="/assets")
   P:auto:(class.approvals>=10)&&(class.rejections==0)
   BECAUSE (robots.unanchored_disallow==true)&&(robots.prefix=="/assets")
```

Three deterministic pieces, no model in any of them:

- **Identity** is the canonical minified statement, so "the same action" is
  string equality over a grammar-defined form.
- **The precondition** is the `BECAUSE` clause, re-evaluated against fresh state
  at apply time. An action computed before someone else edited the file fails
  its own guardrail instead of overwriting them.
- **The automation rule** is the `P:` clause, evaluated by the same expression
  evaluator. It is text: auditable, diffable, tightenable without code.

The class statement carries only signature fields — `prefix`, not `file` — so
the same decision in ten differently-laid-out repositories is ten data points
for one class rather than ten classes of one.

Operations prefer turning on a system that already exists over reimplementing
it. `enable_dependabot` writes the config that starts dependency updates rather
than editing manifests itself, because Dependabot already regenerates lockfiles
correctly per ecosystem and already runs CI on what it proposes.

```bash
foreman actions --propose     # what could be done, with each class's record
foreman approve 11            # re-checks the guardrail, then applies
foreman reject 11             # records the rejection, dismisses the finding
foreman precision             # which rules earn their findings
```

```
#11 p10 anchor_asset_disallow
   files    public/robots.txt
   record   approved 10/10 across 10 project(s) · patch identical to 10 of them
   auto     eligible under policy
```

**Applying and verifying are different claims.** An operation declares whether
it can break a build. Editing a robots.txt cannot, so approvals are evidence
enough; changing a dependency constraint can, so its policy reads
`class.verified` instead — and one broken build disqualifies the class however
many approvals precede it. `foreman verify` asks each project's checks about the
actions applied to it, and an action whose checks have not run is left
unjudged rather than counted either way. Absence of evidence is not evidence.

Two further guards worth knowing about. A policy-approved action is recorded as
`decided_by='policy:auto'` and **excluded from class statistics**, so automation
can never become evidence for more automation. And a stale action is recorded as
`stale` with no decision at all — refusing it is not a rejection, and must not
enter the ledger as a judgement nobody made.

## Drift

```bash
foreman diff            # decisive changes since the last snapshot
foreman diff --all      # including measurements below the noise tolerance
```

A set comparison over `(subject, key, value)` triples, which is exactly why
collectors emit that shape: one differ covers every collector that exists and
every one that does not yet.

Measurements are tolerance-tested and everything else compared exactly. LCP
moving 604ms to 640ms is noise, and a report that fires every night is a report
you stop reading — that is the failure mode worth designing out, not the missed
36ms. A certificate counting down one day at a time is likewise not news; a
renewal is.

## Asking it things

```bash
foreman ask "what are the two most important things for me to do today?"
```

Same thing in the dashboard's left pane ("Ask Foreman", or Escape to collapse). The whole portfolio state — projects,
open findings, pending actions with their class evidence, rule precision — is a
few thousand tokens, so it is assembled and handed over rather than offered as
tools to go and fetch. That costs one round trip and removes every question
about what it actually looked at.

**It cannot approve anything.** Actions are yours, and the ledger exists to make
that decision well-founded rather than to delegate it. An assistant able to
approve its own suggestions would make the approval record measure its own
confidence instead of yours. It can *suggest* one and say why; the suggestion
arrives as a button.

Every turn is stored with what it rested on — the finding, action and project
ids the answer used. An answer with no references is an opinion, and the
difference has to survive into storage. Those references are also the edges
these conversations become once the store is a graph.

## The dashboard

`foreman serve` reads `foreman.db` directly, so the page always shows the last
run. It binds loopback only: the database names real client projects and one
endpoint triggers crawls.

Four tabs over the same data the CLI reads: **Findings**, **Actions**,
**Drift** and **Rules**. Filter by severity, project or free text; click a
finding for every affected subject; hit "Run sweep" for a live log.

The Actions tab is the trust ladder made visible — each pending action shows its
class's record, its canonical SAG statement verbatim, and whether it has earned
its policy. Approving asks first, because it writes to a working tree. "Apply
earned" dry-runs before confirming: a prompt that cannot say how many files it
will touch is not a confirmation. An action whose target moved since it was
computed is marked stale with its Approve button disabled, rather than quietly
applying to a file nobody re-read. Severity is encoded as **shape + label +
colour** — the status palette's medium and low steps measure 13.6 ΔE apart, below
the threshold at which full-colour vision separates them reliably, so colour is
never load-bearing.

## Next

1. More ops. Three exist. Each is small — a precondition, a reason expression,
   and a deterministic render — and the ledger is only as useful as the number
   of classes accumulating evidence.
2. Drift-driven selection: propose and audit where something *changed*, rather
   than re-deriving the same findings nightly. `foreman diff` and
   `foreman precision` are the inputs; nothing consumes them yet.
3. More collectors — Search Console, CrUX, `osv-scanner`, `nuclei`.
