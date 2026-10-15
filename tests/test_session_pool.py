"""HLD §16 D-13(2): each MCP call holds its own AriaClient per target.

Runs through the real ``ConnectionManager``; only ``AriaClient`` is replaced by
a fake, so no network is touched. An AriaClient refreshes its token in place,
so two calls sharing one would race on that token — these tests pin that a
call never shares its client while it runs.
"""

from __future__ import annotations

import itertools
import threading
from typing import Any

import pytest
from vmware_policy.pool import call_scope

from vmware_aria import connection
from vmware_aria.config import AppConfig, TargetConfig
from vmware_aria.connection import ConnectionManager

_WAIT = 5.0


class FakeClient:
    """Stands in for AriaClient; counts concurrent users to catch sharing."""

    _ids = itertools.count(1)

    def __init__(self, target: TargetConfig, password: str, username: str | None = None) -> None:
        self.id = next(self._ids)
        self.host = target.host
        self.alive = True
        self.closed = False
        self._users = 0
        self.max_users = 0
        self._lock = threading.Lock()

    def is_alive_cached(self) -> bool:
        return self.alive

    def close(self) -> None:
        self.closed = True

    def begin(self) -> None:
        with self._lock:
            self._users += 1
            self.max_users = max(self.max_users, self._users)

    def end(self) -> None:
        with self._lock:
            self._users -= 1


@pytest.fixture
def made(monkeypatch: pytest.MonkeyPatch) -> list[FakeClient]:
    clients: list[FakeClient] = []

    def factory(target: TargetConfig, password: str, username: str | None = None) -> FakeClient:
        client = FakeClient(target, password, username)
        clients.append(client)
        return client

    monkeypatch.setattr(connection, "AriaClient", factory)
    monkeypatch.setattr(TargetConfig, "get_password", lambda self, name: "pw")
    monkeypatch.delenv("VMWARE_MCP_POOL_SIZE", raising=False)
    return clients


def _manager() -> ConnectionManager:
    return ConnectionManager(
        AppConfig(
            targets={
                "prod": TargetConfig(host="prod.example", username="u"),
                "lab": TargetConfig(host="lab.example", username="u"),
            },
            default_target="prod",
        )
    )


def _run_calls(*bodies: Any) -> list[Any]:
    """Run each body in its own thread inside its own call scope; return results."""
    results: list[Any] = [None] * len(bodies)
    errors: list[BaseException] = []

    def runner(i: int, body: Any) -> None:
        try:
            with call_scope():
                results[i] = body()
        except BaseException as exc:  # surfaced to the test below
            errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i, b)) for i, b in enumerate(bodies)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(_WAIT * 2)
    assert not any(t.is_alive() for t in threads), "a call never finished"
    if errors:
        raise errors[0]
    return results


def test_concurrent_calls_on_one_target_never_share_a_client(made: list[FakeClient]) -> None:
    mgr = _manager()
    both_hold = threading.Barrier(2, timeout=_WAIT)

    def call() -> FakeClient:
        client = mgr.connect("prod")
        client.begin()
        try:
            both_hold.wait()  # both calls hold their client at the same moment
            assert mgr.connect("prod") is client, "a call keeps one client per target"
        finally:
            client.end()
        return client

    first, second = _run_calls(call, call)
    assert first is not second, "two running calls were handed the same AriaClient"
    assert all(c.max_users == 1 for c in made), "a client carried two calls at once"


def test_a_long_call_leaves_the_other_client_usable(made: list[FakeClient]) -> None:
    mgr = _manager()
    long_holds = threading.Event()
    release_long = threading.Event()

    def long_call() -> FakeClient:
        client = mgr.connect("prod")
        long_holds.set()
        assert release_long.wait(_WAIT)
        return client

    def short_call() -> FakeClient:
        assert long_holds.wait(_WAIT)
        try:
            return mgr.connect("prod")
        finally:
            release_long.set()

    long_client, short_client = _run_calls(long_call, short_call)
    assert short_client is not long_client


def test_calls_on_different_targets_overlap(made: list[FakeClient], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VMWARE_MCP_POOL_SIZE", "1")
    mgr = _manager()
    overlap = threading.Barrier(2, timeout=_WAIT)  # breaks (raises) if they cannot overlap

    def on(target: str) -> Any:
        def call() -> FakeClient:
            client = mgr.connect(target)
            overlap.wait()
            return client

        return call

    prod, lab = _run_calls(on("prod"), on("lab"))
    assert (prod.host, lab.host) == ("prod.example", "lab.example")


def test_outside_a_call_scope_the_same_client_is_reused(made: list[FakeClient]) -> None:
    mgr = _manager()
    first = mgr.connect("prod")
    assert mgr.connect("prod") is first
    assert len(made) == 1


def test_dead_client_is_closed_and_replaced(made: list[FakeClient]) -> None:
    mgr = _manager()
    first = mgr.connect("prod")
    first.alive = False
    second = mgr.connect("prod")
    assert second is not first
    assert first.closed, "the dead client's token and HTTP pool must be released"
    assert not second.closed

    # Same inside a call scope: a dead pooled client is closed and replaced.
    pooled = _run_calls(lambda: mgr.connect("prod"))[0]
    pooled.alive = False
    replacement = _run_calls(lambda: mgr.connect("prod"))[0]
    assert replacement is not pooled and pooled.closed


def test_no_target_pools_under_the_default_target(made: list[FakeClient]) -> None:
    mgr = _manager()
    client = mgr.connect()
    assert mgr.connect("prod") is client
    assert mgr.list_connected() == ["prod"]
    mgr.disconnect_all()
    assert client.closed and mgr.list_connected() == []


def test_unknown_target_teaches_before_any_login(made: list[FakeClient]) -> None:
    with pytest.raises(ValueError, match="Available: prod, lab"):
        _manager().connect("nope")
    assert made == []
