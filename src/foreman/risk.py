"""Which pull requests deserve the expensive look.

An adversarial review costs up to six dollars and reads a diff from four
angles. It has found real bugs — and it has also been run on a dependency
bump. The question this answers is the one asked before spending that:
*does this pull request look like the ones that turned out to need it?*

Two sources of judgement, blended:

**A prior** from what the diff touches. Auth, payments, migrations and infra
have a floor no amount of similarity lowers; a docs-only change has a ceiling.
This is the part that works on day one.

**Neighbours** from history. Every pull request Foreman has seen is embedded
locally (nomic-embed-text through Ollama — nothing leaves the machine) and
stored as a vector in the same store as everything else, under shoal's own
vector column family so its index trainer can pick them up as they are. A
merged pull request earns a label from what happened after it: a deep review
that raised something somebody acted on, or a fix landing on the same files
within two weeks, is *trouble*; two quiet weeks is *clean*. A new pull request
is scored by what its nearest labelled neighbours turned out to be. No label
is ever typed in; the labels are the ledger.

The score is advice. Nothing here spends money or routes anything; it says
"this one" and why, and the board shows it next to the review button.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

SPACE = "pull"
MODEL = os.environ.get("FOREMAN_EMBED_MODEL", "nomic-embed-text")
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
# Neighbours consulted, and how many days after merging a pull request is
# judged by. Two weeks: long enough for a regression to be noticed and fixed,
# short enough that the fix is about this change and not the next one.
NEIGHBOURS = 8
JUDGE_AFTER_DAYS = 14
# Merged pull requests older than this are not fetched for labelling. The
# history exists further back; the API calls to recover their file lists are
# the cost being bounded.
HISTORY_DAYS = 120
FETCH_PER_SWEEP = 40

# What a path says about the change. Order matters only for the label shown;
# every bucket a path matches counts.
BUCKETS: dict[str, re.Pattern[str]] = {
    "auth": re.compile(r"auth|login|session|jwt|oauth|password|passwd|token|permission|rbac", re.I),
    "payments": re.compile(r"pay|billing|stripe|plaid|invoice|checkout|subscription", re.I),
    "secrets": re.compile(r"\.env|secret|credential|keyring|vault", re.I),
    "data": re.compile(r"migration|alembic|schema|models?/|\.sql$|database", re.I),
    "infra": re.compile(
        r"helm|charts?/|terraform|\.tf$|dockerfile|k8s|kube|deploy|nginx|ingress|gateway", re.I
    ),
    "ci": re.compile(r"\.github/workflows|\.gitlab-ci|jenkins|circleci", re.I),
    "deps": re.compile(
        r"package(-lock)?\.json|requirements.*\.txt|pyproject\.toml|poetry\.lock|go\.(mod|sum)"
        r"|pom\.xml|build\.gradle|cargo\.(toml|lock)|yarn\.lock",
        re.I,
    ),
    "tests": re.compile(r"(^|/)tests?(/|_)|_test\.|\.test\.|spec\.", re.I),
    "docs": re.compile(r"\.md$|\.rst$|(^|/)docs?/|changelog|license", re.I),
}
# Buckets that set a floor: a change here is never "low" on similarity alone.
SENSITIVE = {"auth", "payments", "secrets", "data"}
FIX_WORDS = re.compile(
    r"\b(fix|fixes|fixed|revert|hotfix|regress|regression|broke|broken|bug)\b", re.I
)


def buckets(paths: list[str]) -> list[str]:
    out: list[str] = []
    for name, pattern in BUCKETS.items():
        if any(pattern.search(p) for p in paths):
            out.append(name)
    return out


def prior(found: list[str], files: int, changes: int, author: str = "", bot: bool = False) -> float:
    """Risk from the shape of the change alone, in [0, 1]."""
    only = set(found)
    if only and only <= {"docs"}:
        return 0.05
    if only and only <= {"docs", "tests"}:
        return 0.1
    if bot and only <= {"deps", "ci", "docs", "tests"}:
        # A dependency bump from a bot is the review most often wasted.
        return 0.15
    score = 0.2
    if only & {"auth", "payments", "secrets"}:
        score += 0.35
    if "data" in only:
        score += 0.2
    if "infra" in only:
        score += 0.15
    if "ci" in only:
        score += 0.1
    if files > 20 or changes > 500:
        score += 0.15
    elif files > 8 or changes > 200:
        score += 0.05
    return min(score, 1.0)


def descriptor(title: str, slug: str, paths: list[str], found: list[str], author: str) -> str:
    """The text that is embedded. Paths first after the title: what a change
    touches is most of what makes two changes alike."""
    shown = paths[:60]
    return (
        f"{title}\n{slug} by {author}\n"
        f"touches: {', '.join(found) or 'none'}\n"
        f"files: {' '.join(shown)}"
    )


def embed(text: str, timeout: float = 60.0) -> list[float] | None:
    """One embedding from the local model, or None when Ollama is not there.

    None rather than an exception: the prior still stands, and the board says
    'prior only' instead of showing nothing.
    """
    body = json.dumps({"model": MODEL, "prompt": text}).encode()
    req = urllib.request.Request(
        f"{OLLAMA}/api/embeddings", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            payload = json.loads(res.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    vector = payload.get("embedding")
    return [float(x) for x in vector] if isinstance(vector, list) and vector else None


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def label(
    subject: str,
    merged_at: str | None,
    files: list[str],
    later: list[dict[str, Any]],
    review_hits: int,
    now: datetime | None = None,
) -> str:
    """trouble / clean / unknown for one merged pull request.

    `later` is every merged pull request in the same repository after this
    one, as {merged_at, title, files}. A fix landing on the same files within
    the window is the strongest signal there is that this one needed a closer
    look, and it costs nothing — the sweep already knows both.
    """
    merged = _parse(merged_at)
    if merged is None:
        return "unknown"
    now = now or datetime.now(UTC)
    if review_hits:
        return "trouble"
    touched = set(files)
    horizon = merged + timedelta(days=JUDGE_AFTER_DAYS)
    for other in later:
        when = _parse(other.get("merged_at"))
        if when is None or when <= merged or when > horizon:
            continue
        if touched & set(other.get("files") or []) and FIX_WORDS.search(
            str(other.get("title") or "")
        ):
            return "trouble"
    if now < horizon:
        return "unknown"  # the fortnight is not up
    return "clean"


def score(prior_value: float, neighbours: list[dict[str, Any]]) -> dict[str, Any]:
    """Blend the prior with what the labelled neighbours turned out to be."""
    judged = [n for n in neighbours if n.get("meta", {}).get("label") in {"trouble", "clean"}]
    if not judged:
        value = prior_value
        basis = "prior only — no judged neighbours yet"
    else:
        weight = sum(max(n["score"], 0.0) for n in judged) or 1.0
        trouble = (
            sum(max(n["score"], 0.0) for n in judged if n["meta"]["label"] == "trouble") / weight
        )
        value = 0.5 * prior_value + 0.5 * trouble
        troubled = sum(1 for n in judged if n["meta"]["label"] == "trouble")
        basis = f"{troubled} of {len(judged)} similar merged changes needed a fix"
    level = "high" if value >= 0.6 else "medium" if value >= 0.35 else "low"
    return {
        "score": round(value, 3),
        "level": level,
        "basis": basis,
        "neighbours": [
            {
                "key": n["key"],
                "title": n.get("meta", {}).get("title", ""),
                "label": n.get("meta", {}).get("label", "unknown"),
                "similarity": round(float(n["score"]), 3),
                "url": n.get("meta", {}).get("url", ""),
            }
            for n in neighbours[:3]
        ],
    }


# --- the sweep step -------------------------------------------------------------


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode()).hexdigest()[:16]


def _slug_of(url: str) -> str:
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/\d+", url or "")
    return m.group(1) if m else ""


def _review_hits(store: Any) -> dict[str, int]:
    """Per pull request: deep-review findings somebody acted on."""
    hits: dict[str, int] = {}
    for f in store.open_findings():
        if str(f.get("source") or "").startswith("agent:review") and f.get("outcome") == "acted":
            for subject in json.loads(f.get("subjects") or "[]"):
                hits[str(subject)] = hits.get(str(subject), 0) + 1
    return hits


def _pull_facts(store: Any, project: str) -> dict[str, dict[str, str | None]]:
    out: dict[str, dict[str, str | None]] = {}
    for row in store.latest_observations(project):
        subject = str(row["subject"])
        if subject.startswith("pull:"):
            out.setdefault(subject, {})[str(row["key"])] = row["value"]
    return out


def refresh(
    store: Any,
    registry: Any,
    log: Any = lambda _: None,
    now: datetime | None = None,
    fetch: Any = None,
    embedder: Any = embed,
) -> dict[str, int]:
    """Embed what is open, recover what merged, label what is old enough,
    score what is open. One pass; every step is idempotent.

    Returns counts, so the sweep log can say what it did.
    """
    if fetch is None:
        from .collectors.github import GitHubError, gh_api_blocking

        def fetch(path: str) -> Any:
            try:
                return gh_api_blocking(path, paginate=True)
            except GitHubError as exc:
                log(f"risk: {path}: {exc}")
                return None

    now = now or datetime.now(UTC)
    counts = {"embedded": 0, "fetched": 0, "labelled": 0, "scored": 0, "no_embedder": 0}
    known = {r["key"]: r for r in store.vectors(SPACE)}
    hits = _review_hits(store)

    # 1. Merged pull requests from the event log: a vector each, files
    #    recovered from the API when the open-time sweep never saw them.
    since = (now - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    fetched = 0
    for ev in store.events(kind="pr", since=since, limit=5000):
        fields = ev.get("fields")
        if isinstance(fields, str):
            try:
                fields = json.loads(fields or "{}")
            except ValueError:
                fields = {}
        fields = fields or {}
        if str(fields.get("merged")) != "true":
            continue
        slug = _slug_of(str(ev.get("url") or ""))
        if not slug:
            continue
        key = f"pull:{slug}#{ev.get('ref')}"
        rec = known.get(key)
        meta = dict(rec["meta"]) if rec else {}
        if meta.get("state") == "merged" and meta.get("files") is not None:
            continue
        if rec is None:
            if fetched >= FETCH_PER_SWEEP:
                continue
            files = fetch(f"repos/{slug}/pulls/{ev.get('ref')}/files?per_page=100")
            fetched += 1
            if files is None:
                continue
            paths = [str(f.get("filename") or "") for f in files if isinstance(f, dict)][:300]
            found = buckets(paths)
            title = str(ev.get("title") or "")
            author = str(ev.get("actor") or "")
            text = descriptor(title, slug, paths, found, author)
            vector = embedder(text)
            if vector is None:
                counts["no_embedder"] += 1
                continue
            counts["embedded"] += 1
            meta = {
                "project": ev.get("project"),
                "slug": slug,
                "number": str(ev.get("ref")),
                "title": title,
                "url": ev.get("url") or "",
                "author": author,
                "files": paths[:100],
                "buckets": found,
                "digest": _digest(text),
            }
        else:
            vector = rec["vector"]
        meta.update({"state": "merged", "merged_at": fields.get("merged_at") or ev.get("at")})
        meta.setdefault("label", "unknown")
        store.put_vector(SPACE, key, vector, meta)
        known[key] = {"key": key, "vector": vector, "meta": meta}
    counts["fetched"] = fetched

    # 2. Labels, for merged pull requests whose fortnight has passed.
    merged = [r for r in known.values() if r["meta"].get("state") == "merged"]
    by_slug: dict[str, list[dict[str, Any]]] = {}
    for r in merged:
        by_slug.setdefault(str(r["meta"].get("slug")), []).append(r)
    for r in merged:
        meta = r["meta"]
        if meta.get("label") in {"trouble", "clean"}:
            continue
        later = [
            {
                "merged_at": o["meta"].get("merged_at"),
                "title": o["meta"].get("title"),
                "files": o["meta"].get("files") or [],
            }
            for o in by_slug.get(str(meta.get("slug")), [])
            if o["key"] != r["key"]
        ]
        verdict = label(
            r["key"],
            meta.get("merged_at"),
            meta.get("files") or [],
            later,
            hits.get(r["key"], 0),
            now,
        )
        if verdict != meta.get("label"):
            meta["label"] = verdict
            store.put_vector(SPACE, r["key"], r["vector"], meta)
            counts["labelled"] += 1

    # 3. Open pull requests: embed (when the descriptor changed), then score
    #    against merged neighbours.
    for target in registry.active:
        if getattr(target, "github", None) is None:
            continue
        for subject, facts in _pull_facts(store, target.id).items():
            if not facts.get("url") or facts.get("files") is None:
                continue
            try:
                paths = [str(p) for p in json.loads(facts.get("files") or "[]")]
            except ValueError:
                paths = []
            found = buckets(paths)
            author = str(facts.get("author") or "")
            try:
                files_n = int(facts.get("changed_files") or len(paths))
                changes = int(facts.get("additions") or 0) + int(facts.get("deletions") or 0)
            except ValueError:
                files_n, changes = len(paths), 0
            pr = prior(found, files_n, changes, author, bot=author.endswith("[bot]"))
            text = descriptor(
                str(facts.get("title") or ""),
                str(subject.removeprefix("pull:").split("#")[0]),
                paths,
                found,
                author,
            )
            digest = _digest(text)
            rec = known.get(subject)
            if rec and rec["meta"].get("digest") == digest and rec["vector"]:
                vector = rec["vector"]
            else:
                vector = embedder(text)
                if vector is None:
                    counts["no_embedder"] += 1
                    continue
                counts["embedded"] += 1
            try:
                found_near = store.nearest(SPACE, vector, NEIGHBOURS + 1)
            except Exception as exc:  # noqa: BLE001 — a store without vector search still has a prior
                if not counts.get("no_search"):
                    log(f"risk: vector search unavailable, prior only: {exc}")
                counts["no_search"] = counts.get("no_search", 0) + 1
                found_near = []
            neighbours = [
                n
                for n in found_near
                if n["key"] != subject and n.get("meta", {}).get("state") == "merged"
            ][:NEIGHBOURS]
            result = score(pr, neighbours)
            meta = {
                "project": target.id,
                "slug": subject.removeprefix("pull:").split("#")[0],
                "number": subject.split("#")[-1],
                "title": facts.get("title") or "",
                "url": facts.get("url") or "",
                "author": author,
                "files": paths[:100],
                "buckets": found,
                "prior": pr,
                "digest": digest,
                "state": "open",
                "risk": result,
                "scored_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            if rec and rec["meta"].get("state") == "merged":
                continue  # merged since the facts were read; the event pass owns it now
            store.put_vector(SPACE, subject, vector, meta)
            known[subject] = {"key": subject, "vector": vector, "meta": meta}
            counts["scored"] += 1
    log(
        f"risk: {counts['scored']} open scored, {counts['embedded']} embedded, "
        f"{counts['fetched']} merged fetched, {counts['labelled']} labelled"
        + (f", {counts['no_embedder']} skipped (no embedder)" if counts["no_embedder"] else "")
    )
    return counts


def risks(store: Any) -> dict[str, dict[str, Any]]:
    """subject -> the last risk computed for an open pull request."""
    return {
        r["key"]: {
            **r["meta"]["risk"],
            "buckets": r["meta"].get("buckets", []),
            "scored_at": r["meta"].get("scored_at"),
        }
        for r in store.vectors(SPACE)
        if r["meta"].get("state") == "open" and isinstance(r["meta"].get("risk"), dict)
    }
