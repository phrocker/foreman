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
