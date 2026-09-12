"""Credentials.

Foreman held no secret at all until providers needed reading directly. The
property worth protecting is narrow and absolute: a credential goes in and never
comes back out anywhere a browser, a log or the graph can see it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from foreman import secrets
from foreman.web import create_app

SECRET = "gd-live-0123456789abcdefWXYZ"


@pytest.fixture
def vault(monkeypatch):
    """An in-memory keyring, so tests never touch the operator's real one."""
    store: dict[tuple[str, str], str] = {}

    class Fake:
        def set_password(self, service, name, value):
            store[(service, name)] = value

        def get_password(self, service, name):
            return store.get((service, name))

        def delete_password(self, service, name):
            from keyring.errors import PasswordDeleteError

            if (service, name) not in store:
                raise PasswordDeleteError(name)
            del store[(service, name)]

    monkeypatch.setattr(secrets, "_backend", lambda: (Fake(), "tests.Fake"))
    return store


# --- the store --------------------------------------------------------------


def test_a_credential_round_trips(vault):
    secrets.set_secret("godaddy_api_key", SECRET)
    assert secrets.get_secret("godaddy_api_key") == SECRET


def test_status_says_whether_it_is_set_without_saying_what_it_is(vault):
    secrets.set_secret("godaddy_api_key", SECRET)
    status = secrets.status("godaddy_api_key")
    assert status.set is True
    assert status.hint == "WXYZ"
    assert SECRET not in str(status)


def test_a_short_credential_gets_no_hint(vault):
    """Four characters of a twelve-character secret is a third of it."""
    secrets.set_secret("github_token", "short")
    assert secrets.status("github_token").hint is None


def test_an_unset_credential_is_not_an_error(vault):
    assert secrets.status("github_token") == secrets.Status("github_token", False)


def test_an_empty_credential_is_refused(vault):
    with pytest.raises(ValueError):
        secrets.set_secret("github_token", "")


def test_removing_says_whether_there_was_anything_to_remove(vault):
    secrets.set_secret("github_token", SECRET)
    assert secrets.delete_secret("github_token") is True
    assert secrets.delete_secret("github_token") is False
    assert secrets.status("github_token").set is False


# --- the refusal ------------------------------------------------------------


def test_a_plaintext_backend_is_refused_rather_than_used(monkeypatch):
    """keyrings.alt writes these to a file in the home directory. A credential
    store that quietly stops being one is worse than an obvious file, because
    nobody looks at it twice."""

    class Plaintext:
        pass

    Plaintext.__module__ = "keyrings.alt.file"
    Plaintext.__qualname__ = Plaintext.__name__ = "PlaintextKeyring"

    import keyring

    monkeypatch.setattr(keyring, "get_keyring", lambda: Plaintext())
    with pytest.raises(secrets.SecretsUnavailable, match="in the clear"):
        secrets.backend_name()
    assert secrets.available() is False


# --- the API ----------------------------------------------------------------


@pytest.fixture
def client(vault, tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db"))


def test_the_settings_endpoint_never_returns_a_value(client, vault):
    secrets.set_secret("godaddy_api_key", SECRET)
    body = client.get("/api/settings").json()
    assert SECRET not in str(body)
    entry = next(c for c in body["credentials"] if c["name"] == "godaddy_api_key")
    assert entry["set"] is True and entry["hint"] == "WXYZ"


def test_saving_a_credential_answers_with_its_status_not_its_value(client, vault):
    body = client.put("/api/settings/godaddy_api_key", json={"value": SECRET}).json()
    assert body == {"name": "godaddy_api_key", "set": True, "hint": "WXYZ"}
    assert secrets.get_secret("godaddy_api_key") == SECRET


def test_there_is_no_endpoint_that_reads_a_credential_back(client, vault):
    """The whole design rests on this. A GET that returned a value would put it
    in a browser history and in every proxy between here and there."""
    secrets.set_secret("godaddy_api_key", SECRET)
    assert client.get("/api/settings/godaddy_api_key").status_code == 405


def test_an_unknown_credential_is_refused(client, vault):
    assert client.put("/api/settings/nonsense", json={"value": "x"}).status_code == 404
    assert client.delete("/api/settings/nonsense").status_code == 404


def test_an_empty_value_is_refused(client, vault):
    assert client.put("/api/settings/github_token", json={"value": "  "}).status_code == 400


def test_a_credential_can_be_removed_from_where_it_was_added(client, vault):
    client.put("/api/settings/github_token", json={"value": SECRET})
    assert client.delete("/api/settings/github_token").json()["removed"] is True
    assert secrets.status("github_token").set is False
