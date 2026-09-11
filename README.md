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
- **No Postgres.** Single operator, single machine. SQLite in WAL mode.
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

## The dashboard

`foreman serve` reads `foreman.db` directly, so the page always shows the last
run. It binds loopback only: the database names real client projects and one
endpoint triggers crawls.

Filter by severity, project, or free text; click a finding for every affected
subject; hit "Run sweep" for a live log. Severity is encoded as **shape + label +
colour** — the status palette's medium and low steps measure 13.6 ΔE apart, below
the threshold at which full-colour vision separates them reliably, so colour is
never load-bearing.

## Next

1. `foreman diff` — compare snapshots, so drift is visible as change over time.
2. Outcome tracking — record whether a finding was acted on or dismissed, so rule
   precision and cost-per-finding become measurable rather than assumed.
3. Data-driven selection — audit the projects that drifted, or whose findings you
   actually act on, instead of on a blind schedule.
4. More collectors — Search Console, CrUX, `osv-scanner`, `nuclei`.
