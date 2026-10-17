"""One DNS lookup per host: everything that fetches shares the answer.

A sweep's thousand lookups a minute tripped the operator's own resolver. The
cache under socket.getaddrinfo answers the second request for a host from
the first, across ports, and remembers a failure briefly so a host that does
not resolve is not asked about once per page.
"""

from __future__ import annotations

import socket

import pytest

from foreman import net


@pytest.fixture
def fake_resolver(monkeypatch):
    calls: list[tuple] = []

    def resolver(host, port, family=0, type=0, proto=0, flags=0):
        calls.append((host, port))
        if host == "nowhere.test":
            raise socket.gaierror(-2, "Name or service not known")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.9", port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::9", port, 0, 0)),
        ]

    net.uninstall()
    monkeypatch.setattr(net, "_resolve", resolver)
    net.reset_stats()
    net.install()
    yield calls
    net.uninstall()


def test_a_host_is_resolved_once_and_served_for_every_port(fake_resolver):
    a = socket.getaddrinfo("Example.test", 443, type=socket.SOCK_STREAM)
    b = socket.getaddrinfo("example.test", 80, type=socket.SOCK_STREAM)
    c = socket.getaddrinfo("example.test", 443, type=socket.SOCK_STREAM)
    assert len(fake_resolver) == 1, "the resolver was asked more than once for one host"
    assert fake_resolver[0] == ("Example.test", 0)  # resolved for every port at once
    assert [e[4][1] for e in a] == [443, 443]
    assert [e[4][1] for e in b] == [80, 80]
    assert b[1][4] == ("2001:db8::9", 80, 0, 0)  # the v6 sockaddr keeps its shape
    assert a == c
    s = net.stats()
    assert (s.lookups, s.resolved, s.served) == (3, 1, 2)
    assert "1 lookup(s) resolved, 2 served from cache across 3 request(s)" in s.line()


def test_a_failure_is_remembered_briefly_rather_than_retried_per_page(fake_resolver):
    for _ in range(5):
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("nowhere.test", 443)
    assert len(fake_resolver) == 1


def test_answers_expire_and_clear_forgets_them(fake_resolver, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(net.time, "monotonic", lambda: now[0])
    socket.getaddrinfo("example.test", 443)
    socket.getaddrinfo("example.test", 443)
    assert len(fake_resolver) == 1
    now[0] += net.TTL_S + 1
    socket.getaddrinfo("example.test", 443)
    assert len(fake_resolver) == 2, "an answer older than the TTL was served"
    net.clear()
    socket.getaddrinfo("example.test", 443)
    assert len(fake_resolver) == 3


def test_a_service_name_port_is_passed_through_untouched(fake_resolver):
    socket.getaddrinfo("example.test", "https")
    assert fake_resolver[0] == ("example.test", "https")


def test_install_is_idempotent_and_uninstall_restores():
    net.install()
    net.install()
    assert socket.getaddrinfo is net._cached_getaddrinfo
    net.uninstall()
    assert socket.getaddrinfo is net._system
