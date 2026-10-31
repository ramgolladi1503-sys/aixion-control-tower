from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from app.models import now_utc
from app.postgres_external_agent_auth_repository import (
    PostgresExternalAgentAuthRepository,
)

from test_postgres_external_agent_auth_repository import _agent_and_credential, _create_agent

pytest_plugins = ("postgres_external_agent_auth_fixtures",)


class _PausingCursor:
    def __init__(self, cursor, selected: Event, resume: Event) -> None:
        self._cursor = cursor
        self._selected = selected
        self._resume = resume

    def fetchone(self):
        row = self._cursor.fetchone()
        self._selected.set()
        if not self._resume.wait(timeout=10):
            raise TimeoutError("Timed out waiting for concurrent credential update")
        return row


class _PausingConnection:
    def __init__(self, connection, selected: Event, resume: Event) -> None:
        self._connection = connection
        self._selected = selected
        self._resume = resume
        self._paused = False

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._connection.__exit__(exc_type, exc_value, traceback)

    def execute(self, query, params=None):
        cursor = self._connection.execute(query, params)
        if not self._paused and "FROM external_agents" in query:
            self._paused = True
            return _PausingCursor(cursor, self._selected, self._resume)
        return cursor


def test_get_observes_one_snapshot_during_concurrent_agent_update(
    agent_repository,
) -> None:
    base_repository, connect, workspace_id, owner_id = agent_repository
    agent, credential, _ = _agent_and_credential(workspace_id, owner_id, rate_limit=10)
    _create_agent(base_repository, agent, credential)

    selected = Event()
    resume = Event()

    def pausing_connect():
        return _PausingConnection(connect(), selected, resume)

    reader_repository = PostgresExternalAgentAuthRepository(pausing_connect)
    update_time = now_utc()
    changed_agent = agent.model_copy(
        update={
            "secret_hash": "different-token-hash",
            "updated_at": update_time,
        }
    )
    changed_credential = credential.model_copy(
        update={
            "rate_limit_per_minute": 20,
            "updated_at": update_time,
        }
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        read_future = executor.submit(reader_repository.get, agent.id)
        try:
            assert selected.wait(timeout=10)
            base_repository.update(
                agent=changed_agent,
                credential=changed_credential,
            )
        finally:
            resume.set()
        observed = read_future.result(timeout=10)

    assert observed is not None
    observed_agent, observed_credential = observed
    assert (
        observed_agent.secret_hash == agent.secret_hash
        and observed_credential.rate_limit_per_minute == 10
    ) or (
        observed_agent.secret_hash == changed_agent.secret_hash
        and observed_credential.rate_limit_per_minute == 20
    )
