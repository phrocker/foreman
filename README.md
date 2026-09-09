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
```

## Commands

| Command | What it does |
| --- | --- |
| `foreman init` | Create `sites.yaml` from the example |
| `foreman sites` | List registered sites and which are fixable |
| `foreman collect` | Run collectors, store a timestamped snapshot |
| `foreman check` | Evaluate rules against the latest snapshot |
| `foreman status` | Open findings across the portfolio |

`--site` scopes any of them to one site; `--collector` scopes `collect`.

## Next

1. `foreman diff` — compare snapshots, so drift is visible as change over time.
2. Triage: one model call over the diff, ranking findings with reasoning.
3. Fix arm: for sites with a `repo`, hand a finding to Claude Code and open a PR.
4. More collectors — Search Console, CrUX, `osv-scanner`, `nuclei`.
