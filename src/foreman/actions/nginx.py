"""nginx configuration operations."""

from __future__ import annotations

import re
from pathlib import Path

from ..config import Project
from .base import FileEdit, OpNotApplicable, Patch

NGINX_LOCATIONS = (
    "nginx.conf",
    "frontend/nginx.conf",
    "docker/nginx.conf",
    "deploy/nginx.conf",
    "conf/nginx.conf",
    "etc/nginx.conf",
)

# Headers with a single defensible default, and nothing else.
#
# content-security-policy is deliberately absent. A correct CSP depends on which
# origins a page actually loads from, a wrong one silently breaks the site, and
# there is no default that is right for an unknown project. An op that cannot
# compute a correct value for every project it might run on has no business
# being automatable, and this registry is the list of things that can eventually
# stop needing a human.
SAFE_HEADERS = {
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "x-frame-options": "SAMEORIGIN",
}

_SERVER_OPEN = re.compile(r"^(\s*)server\s*\{\s*$")


def _locate(repo: Path) -> Path | None:
    for candidate in NGINX_LOCATIONS:
        path = repo / candidate
        if path.is_file():
            return path
    return None


def _has_header(text: str, header: str) -> bool:
    return bool(re.search(rf"(?im)^\s*add_header\s+{re.escape(header)}\b", text))


def _server_blocks(lines: list[str]) -> list[int]:
    return [i for i, line in enumerate(lines) if _SERVER_OPEN.match(line)]


class AddSecurityHeader:
    """Add one missing security response header to an nginx server block.

    One header per action rather than all of them at once. Each header then
    accumulates its own approval record, so agreeing to `nosniff` a dozen times
    says nothing about HSTS — which is correct, since HSTS is the one that can
    strand a host on https for a year.
    """

    verb = "add_security_header"
    summary = "Add a missing security header to the nginx server block"
    # The header is what makes two of these the same decision. The file is not.
    signature_fields = ("header",)
    answers = ("missing_security_header",)

    def propose(self, project: Project, finding: dict) -> list[dict]:
        if finding.get("rule") not in self.answers or not project.fixable:
            return []
        assert project.repo is not None
        path = _locate(project.repo)
        if path is None:
            return []
        text = path.read_text()
        if len(_server_blocks(text.splitlines())) != 1:
            # Several server blocks means choosing one, and choosing is exactly
            # what a deterministic op must not do.
            return []
        rel = str(path.relative_to(project.repo))
        return [
            {"file": rel, "header": header}
            for header in sorted(SAFE_HEADERS)
            if not _has_header(text, header)
        ]

    def _read(self, project: Project, params: dict) -> tuple[Path, str]:
        assert project.repo is not None
        path = project.repo / params["file"]
        if not path.is_file():
            raise OpNotApplicable(f"{params['file']} does not exist in {project.repo}")
        return path, path.read_text()

    def reason(self, params: dict) -> str:
        return f'(nginx.has_header==false)&&(nginx.header=="{params["header"]}")'

    def state(self, project: Project, params: dict) -> dict:
        header = params["header"]
        if header not in SAFE_HEADERS:
            raise OpNotApplicable(f"{header} has no safe default value")
        _, text = self._read(project, params)
        if len(_server_blocks(text.splitlines())) != 1:
            raise OpNotApplicable("expected exactly one server block")
        return {"nginx": {"has_header": _has_header(text, header), "header": header}}

    def render(self, project: Project, params: dict) -> Patch:
        path, text = self._read(project, params)
        header = params["header"]
        if _has_header(text, header):
            raise OpNotApplicable(f"{header} is already set")

        lines = text.splitlines(keepends=True)
        blocks = _server_blocks([line.rstrip("\n") for line in lines])
        if len(blocks) != 1:
            raise OpNotApplicable("expected exactly one server block")

        index = blocks[0]
        indent = _SERVER_OPEN.match(lines[index].rstrip("\n")).group(1) + "    "
        # `always` so the header is sent on error responses too, not just 2xx.
        addition = f'{indent}add_header {header} "{SAFE_HEADERS[header]}" always;\n'
        out = lines[: index + 1] + [addition] + lines[index + 1 :]
        return Patch(edits=(FileEdit(path=params["file"], before=text, after="".join(out)),))
