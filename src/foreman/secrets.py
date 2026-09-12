"""Credentials, kept in the OS keyring and never anywhere else.

Foreman held no secret at all until now: collectors read through `gh` and
`gcloud`, which hold their own. That broke when a `gcloud` login expired and a
project went unreadable until a human ran `gcloud auth login` — a monitor that
stops monitoring because nobody clicked something. Reading providers directly
means holding their credentials, so this is where they go.

Three rules, and the first is the one everything else rests on.

**Nothing here reads a secret back to a caller that only needs to know it
exists.** `status()` answers set-or-not; `get()` exists for the collector about
to make a request and for nothing else. A dashboard, an API response and a log
line all get the first. A value that can be read back over HTTP is a value in
somebody's browser history.

**A plaintext backend is a hard failure.** `keyrings.alt` will happily write
these to a file in the home directory, and a credential store that quietly
stops being one is worse than an obvious file, because nobody looks at it again.

**The store and the graph never see them.** `foreman.yaml` is gitignored because
it names real client sites; a secret beside it would be one `git add -f` from
being published, and the graph is designed to be read by agents.
"""

from __future__ import annotations

from dataclasses import dataclass

SERVICE = "foreman"

# Backends that put the value on disk in the clear. Present only if somebody
# installed keyrings.alt, which is why this is a refusal rather than a warning:
# the caller asked for a credential store and would otherwise be given a file.
PLAINTEXT_BACKENDS = frozenset(
    {
        "keyrings.alt.file.PlaintextKeyring",
        "keyrings.alt.file_base.Keyring",
        "keyring.backends.fail.Keyring",
    }
)


class SecretsUnavailable(RuntimeError):
    """No usable keyring. Deliberately not a fallback."""


@dataclass(frozen=True)
class Credential:
    """One credential Foreman can be given, and what it unlocks.

    Declared rather than free-form so the settings page can list what is
    missing. A provider nobody has configured should say so, the same way an
    unmonitored surface does — silence reads as "nothing to report".
    """

    name: str
    label: str
    provider: str
    help: str


# Grows as providers arrive. `gh` and `gcloud` are still read through their own
# CLIs, so nothing here is required to run Foreman — these are for providers
# that have no CLI to borrow from, and for unattended operation where a CLI
# login expires and takes the monitoring down with it.
CREDENTIALS: tuple[Credential, ...] = (
    Credential(
        name="godaddy_api_key",
        label="GoDaddy API key",
        provider="godaddy",
        help="No CLI exists, so domains, DNS and expiry are unreadable without this.",
    ),
    Credential(
        name="godaddy_api_secret",
        label="GoDaddy API secret",
        provider="godaddy",
        help="Issued alongside the key. Both are needed.",
    ),
    Credential(
        name="github_token",
        label="GitHub token",
        provider="github",
        help="Optional. Without it GitHub is read through the gh CLI, which works "
        "until its login expires with nobody there to renew it.",
    ),
)


def credentials_for(provider: str) -> tuple[Credential, ...]:
    return tuple(c for c in CREDENTIALS if c.provider == provider)


@dataclass(frozen=True)
class Status:
    """What may be said about a credential without revealing it."""

    name: str
    set: bool
    # The last four characters, so a person can tell which key is loaded without
    # the value being recoverable. Absent for anything short enough that four
    # characters would be most of it.
    hint: str | None = None


def _backend():
    try:
        import keyring
    except ModuleNotFoundError as exc:
        # Its own answer rather than an exception escaping: callers already
        # handle "no usable keyring", and letting this through turned a missing
        # dependency into a 500 on the one page meant to explain the problem.
        raise SecretsUnavailable(
            "the keyring package is not installed, so credentials cannot be "
            "stored. Reinstall Foreman to pick it up."
        ) from exc

    backend = keyring.get_keyring()
    name = f"{type(backend).__module__}.{type(backend).__name__}"
    if name in PLAINTEXT_BACKENDS:
        raise SecretsUnavailable(
            f"the active keyring backend ({name}) stores secrets in the clear. "
            "Install a real one — gnome-keyring or kwallet on Linux — rather than "
            "letting Foreman write credentials to a file."
        )
    return keyring, name


def backend_name() -> str:
    """Which keyring is in use, for the dashboard to show. Never a value."""
    return _backend()[1]


def available() -> bool:
    try:
        _backend()
        return True
    except Exception:
        return False


def set_secret(name: str, value: str) -> None:
    if not value:
        raise ValueError("refusing to store an empty credential")
    keyring, _ = _backend()
    keyring.set_password(SERVICE, name, value)


def get_secret(name: str) -> str | None:
    """The actual value. For the collector about to make a request, only.

    Everything that merely needs to know whether a credential exists calls
    `status()` instead — including the API, the dashboard and any log line.
    """
    keyring, _ = _backend()
    return keyring.get_password(SERVICE, name)


def delete_secret(name: str) -> bool:
    """Remove one. Returns whether there was anything to remove.

    Revocation lives beside storage on purpose: a credential you cannot remove
    from the same place you added it is half a feature, and the half that is
    missing is the one you need in a hurry.
    """
    from keyring.errors import PasswordDeleteError

    keyring, _ = _backend()
    try:
        keyring.delete_password(SERVICE, name)
        return True
    except PasswordDeleteError:
        return False


def status(name: str) -> Status:
    """Whether a credential is set, and a hint at which one it is."""
    try:
        value = get_secret(name)
    except SecretsUnavailable:
        return Status(name=name, set=False)
    if not value:
        return Status(name=name, set=False)
    return Status(name=name, set=True, hint=value[-4:] if len(value) >= 12 else None)


def statuses(names: list[str] | None = None) -> list[Status]:
    return [status(name) for name in (names or [c.name for c in CREDENTIALS])]
