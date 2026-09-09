# Foreman

A portfolio control plane for ~40 websites: crawl them all nightly, diff against
history, and surface the handful of things that actually need attention.

Built to run locally on one machine. It is not a SaaS and shouldn't become one —
the reason it can do things hosted SEO tools can't is that it has your API keys
and your source checkouts.

## Why this exists

Hosted tools report; they can't fix. Foreman knows which sites you have a local
checkout for, so a finding can become a branch and a pull request instead of a
line in a dashboard. That asymmetry is the entire argument for running it
yourself.

## Design

**Collectors never call a model.** Everything under `collectors/` is a crawl, a
socket, or an API read — reproducible, free, and identical across runs. The
first three real findings this tool produced were a robots.txt rule blocking
every JS bundle on a production site, 45 sitemap URLs answering redirects, and
`http://` serving 200 instead of redirecting. All three were found with HTTP
requests and string comparison. Nothing about *finding* them needed intelligence.

**The model works on deltas, not corpora.** Once history exists, the nightly
question is "what changed and does it matter" — a few hundred tokens, not a
re-read of 3,000 pages. Full deep reads happen on a slow rotation, through the
Batch API, where latency is irrelevant and the price is halved.

**One observation shape.** Collectors emit `(subject, key, value)` triples
rather than bespoke tables. That's what lets a single diff engine work across
every collector: a change is a row whose value differs from the previous run's
row with the same `(site, subject, key)`. Add a collector, get drift detection
free.

**Render only to compare.** The crawl collector reads what the server sends,
because that is what a non-JS crawler indexes. The optional `render` collector
reads what a browser produces after JavaScript runs. Where the two disagree you
have found content or metadata that exists only for users — invisible to Bing,
to AI crawlers, and to every social scraper. Plain Playwright with an honest
User-Agent: a stealth browser would defeat the purpose, since the question being
asked is "what does a crawler see".

**Probe for what the sitemap won't admit.** A sitemap crawl only sees pages a
site claims to have, which makes it structurally blind to the most common SPA
defect: unmatched URLs answering 200 with the homepage shell, turning every typo
and scanner probe into an indexable duplicate. Foreman asks for URLs that
shouldn't exist and compares what comes back.

## Deliberately not built

- **No worker fleet, no queue, no provider router.** At 40 sites × ~75 pages,
  collection is ~3 minutes of wall clock and a full model pass is minutes more
  on one machine. Distributing it would be infrastructure in search of a
  bottleneck. `budget.py` is kept regardless — anything that spawns subagents
  needs a ceiling, single-machine or not.
- **No Postgres.** Single operator, single machine. SQLite in WAL mode.
- **No stealth browser.** Foreman crawls sites you own. If a WAF blocks it,
  allowlist it — that is one firewall rule, not a patched Chromium. Fingerprint
  spoofing would also give a view no search engine has, which is the opposite of
  the diagnostic.
- **No second model provider yet.** Worth adding for redundancy and cost
  arbitrage, not for speed — nothing here is rate-limited.

Each of these is a one-line change of mind if the numbers move. None of them are
true at this scale today.

## Quickstart

```bash
uv venv && uv pip install -e ".[dev]"
foreman init                 # writes sites.yaml from the example
$EDITOR sites.yaml
foreman collect              # crawl every site, store a snapshot
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
| `foreman init` | Create `sites.yaml` from the example |
| `foreman sites` | List registered sites and which are fixable |
| `foreman collect` | Run collectors, store a timestamped snapshot |
| `foreman check` | Evaluate rules against the latest snapshot |
| `foreman status` | Open findings across the portfolio |
| `foreman audit [site]` | Escalate to a `/seo` plugin skill via Claude Code |
| `foreman serve` | Interactive dashboard on loopback (default `:8765`) |

`--site` scopes any of them to one site; `--collector` scopes `collect`.

## Escalation: `foreman audit`

Collectors answer "what is true about this site". They cannot answer "is this
content any good", "would an AI search engine cite this page", or "why does this
rank below a competitor" — those need judgement, and the `/seo` plugin already
has specialists for them. Foreman does not reimplement that; it decides *which*
of 40 sites deserve it and hands those few to Claude Code.

```bash
foreman audit mfa --skill seo-page --budget 2.00
foreman audit --skill seo-geo --budget 20.00     # every site
```

The open deterministic findings go into the prompt so the agent skips what the
nightly sweep already knows — the same work-on-the-delta principle the
collectors follow. Results come back as schema-validated JSON, are namespaced
`skill/rule`, and are stored with `source='agent:<skill>'`. That last part
matters twice over: the dashboard badges them, because a judgement call and a
reproducible check deserve different trust, and `foreman check` only ever
deletes `source='rule'` rows, so a nightly sweep cannot wipe results you paid
for.

`--budget` is a real ceiling. Cost comes back in Claude Code's JSON envelope and
is charged to a `Budget`, which gates the *next* site — so a sweep across 40
sites stops rather than running away. This is what `budget.py` was written for.

## The dashboard

`foreman serve` reads `foreman.db` directly, so the page always shows the last
run — no export step, and nothing about your sites leaves the machine. It binds
loopback only: the database names real client sites and one endpoint triggers
crawls.

Filter by severity, site, or free text; click a finding to see every affected URL
and why it matters; click a site to scope to it; hit "Run sweep" to collect and
re-evaluate with a live log. Severity is encoded as **shape + label + colour** —
the status palette's medium and low steps measure only 13.6 ΔE apart, which is
below the threshold at which full-colour vision separates them reliably, so
colour is never load-bearing.

## Next

1. `foreman diff` — compare snapshots, so drift is visible as change over time.
2. Triage: one model call over the diff, ranking findings with reasoning.
3. Fix arm: for sites with a `repo`, hand a finding to Claude Code and open a PR.
4. More collectors — Search Console, CrUX, `osv-scanner`, `nuclei`.
