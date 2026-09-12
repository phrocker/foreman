"""robots.txt operations."""

from __future__ import annotations

import re
from pathlib import Path

from ..config import Project
from .base import FileEdit, OpNotApplicable, Patch

# Where a robots.txt actually lives, by convention, across the stacks in use.
ROBOTS_LOCATIONS = (
    "public/robots.txt",
    "static/robots.txt",
    "frontend/public/robots.txt",
    "web/robots.txt",
    "src/robots.txt",
    "robots.txt",
)
ASSET_PREFIXES = ("/assets", "/static", "/_next", "/dist", "/build")


def _locate(repo: Path) -> Path | None:
    for candidate in ROBOTS_LOCATIONS:
        path = repo / candidate
        if path.is_file():
            return path
    return None


def _declares_sitemap(text: str) -> bool:
    return any(line.lower().startswith("sitemap:") for line in text.splitlines())


def _unanchored(text: str, prefix: str) -> list[str]:
    """Disallow lines that block `prefix` and everything beneath it."""
    hits = []
    for line in text.splitlines():
        m = re.match(r"(?i)\s*disallow:\s*(\S+)\s*$", line)
        if m and m.group(1).rstrip("/") == prefix and not m.group(1).endswith("$"):
            hits.append(line)
    return hits


class AnchorAssetDisallow:
    """Anchor a robots.txt Disallow that is unintentionally blocking a build
    output directory.

    `Disallow: /assets` blocks `/assets/app.js` too, so no crawler can fetch the
    scripts or stylesheets any page needs to render — while the rule was almost
    always written to block a single application route. Anchoring it with `$`
    keeps the intent and unblocks the bundles.
    """

    verb = "anchor_asset_disallow"
    summary = "Anchor an unanchored robots.txt Disallow on an asset directory"
    # The asset prefix is what makes two of these the same action. The file path
    # is not: the same fix to `public/robots.txt` on one project and
    # `frontend/public/robots.txt` on another is the same decision.
    signature_fields = ("prefix",)
    # Editing robots.txt has no build to break.
    requires_verification = False

    # Which finding this op answers. Declared rather than inferred, so adding an
    # op never requires a model to work out where it applies.
    answers = ("robots_blocks_assets",)

    def propose(self, project: Project, finding: dict) -> list[dict]:
        if finding.get("rule") not in self.answers or not project.fixable:
            return []
        assert project.repo is not None
        path = _locate(project.repo)
        if path is None:
            return []
        text = path.read_text()
        rel = str(path.relative_to(project.repo))
        return [
            {"file": rel, "prefix": prefix}
            for prefix in ASSET_PREFIXES
            if _unanchored(text, prefix)
        ]

    def _read(self, project: Project, params: dict) -> tuple[Path, str]:
        assert project.repo is not None
        path = project.repo / params["file"]
        if not path.is_file():
            raise OpNotApplicable(f"{params['file']} does not exist in {project.repo}")
        return path, path.read_text()

    def reason(self, params: dict) -> str:
        # Parenthesised deliberately — see Op.reason.
        return f'(robots.unanchored_disallow==true)&&(robots.prefix=="{params["prefix"]}")'

    def state(self, project: Project, params: dict) -> dict:
        """Read the file now, so a re-check sees the current world."""
        _, text = self._read(project, params)
        prefix = params["prefix"]
        return {
            "robots": {
                "unanchored_disallow": bool(_unanchored(text, prefix)),
                "prefix": prefix,
            }
        }

    def render(self, project: Project, params: dict) -> Patch:
        path, text = self._read(project, params)
        prefix = params["prefix"]
        targets = _unanchored(text, prefix)
        if not targets:
            raise OpNotApplicable(f"no unanchored Disallow on {prefix}")

        out = []
        for line in text.splitlines(keepends=True):
            if line.rstrip("\n") in targets:
                indent = line[: len(line) - len(line.lstrip())]
                newline = "\n" if line.endswith("\n") else ""
                # Anchored so it matches the route exactly, plus an explicit
                # Allow for everything beneath it. Both, because Allow alone
                # relies on longest-match precedence that not every crawler
                # implements the same way.
                out.append(f"{indent}Disallow: {prefix}${newline}")
                out.append(f"{indent}Allow: {prefix}/{newline}")
            else:
                out.append(line)
        return Patch(edits=(FileEdit(path=params["file"], before=text, after="".join(out)),))


class AddSitemapReference:
    """Declare the sitemap in robots.txt.

    The sitemap is at the conventional path and parses; it is simply not
    announced. Crawlers that do not guess the location have to discover every
    URL by following links instead.
    """

    verb = "add_sitemap_reference"
    summary = "Point robots.txt at the sitemap"
    # No signature fields. The sitemap URL differs per project, but "declare the
    # sitemap" is one decision everywhere — parameterising on the URL would give
    # every project its own class of one, and a class of one never accumulates
    # enough evidence to mean anything.
    signature_fields = ()
    requires_verification = False
    answers = ("robots_missing_sitemap",)

    def propose(self, project: Project, finding: dict) -> list[dict]:
        if finding.get("rule") not in self.answers or not project.fixable:
            return []
        if project.web is None:
            return []
        assert project.repo is not None
        path = _locate(project.repo)
        if path is None:
            return []
        if _declares_sitemap(path.read_text()):
            return []
        return [
            {
                "file": str(path.relative_to(project.repo)),
                "sitemap_url": f"{project.web.url}/sitemap.xml",
            }
        ]

    def _read(self, project: Project, params: dict) -> tuple[Path, str]:
        assert project.repo is not None
        path = project.repo / params["file"]
        if not path.is_file():
            raise OpNotApplicable(f"{params['file']} does not exist in {project.repo}")
        return path, path.read_text()

    def reason(self, params: dict) -> str:
        # Invariant across projects, so the class stays one class.
        return "(robots.sitemap_declared==false)"

    def state(self, project: Project, params: dict) -> dict:
        _, text = self._read(project, params)
        return {"robots": {"sitemap_declared": _declares_sitemap(text)}}

    def render(self, project: Project, params: dict) -> Patch:
        _, text = self._read(project, params)
        if _declares_sitemap(text):
            raise OpNotApplicable("robots.txt already declares a sitemap")
        # Appended, and on its own paragraph: Sitemap is a global directive, so
        # putting it inside a User-agent group reads as if it were scoped to one.
        separator = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        after = f"{text}{separator}Sitemap: {params['sitemap_url']}\n"
        return Patch(edits=(FileEdit(path=params["file"], before=text, after=after),))
