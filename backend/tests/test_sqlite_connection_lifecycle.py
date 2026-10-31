import sqlite3

import pytest

from app.store import SQLiteBackedStore


def _store(tmp_path):
    store = object.__new__(SQLiteBackedStore)
    store.db_path = tmp_path / "lifecycle.sqlite3"
    return store


def test_store_connection_context_commits_and_closes(tmp_path):
    store = _store(tmp_path)

    with store._connect() as connection:
        connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
        connection.execute("INSERT INTO records (value) VALUES ('committed')")

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")

    with store._connect() as reopened:
        assert reopened.execute("SELECT value FROM records").fetchone() == ("committed",)


def test_store_connection_context_rolls_back_and_closes_on_exception(tmp_path):
    store = _store(tmp_path)
    with store._connect() as connection:
        connection.execute("CREATE TABLE records (value TEXT NOT NULL)")

    with pytest.raises(RuntimeError, match="force rollback"):
        with store._connect() as connection:
            connection.execute("INSERT INTO records (value) VALUES ('rolled back')")
            raise RuntimeError("force rollback")

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")

    with store._connect() as reopened:
        assert reopened.execute("SELECT value FROM records").fetchall() == []


def test_store_connection_closes_when_commit_fails(tmp_path):
    store = _store(tmp_path)
    with store._connect() as connection:
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE child (parent_id INTEGER, "
            "FOREIGN KEY(parent_id) REFERENCES parent(id) "
            "DEFERRABLE INITIALLY DEFERRED)"
        )

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        with store._connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("INSERT INTO child (parent_id) VALUES (404)")

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")

    with store._connect() as reopened:
        assert reopened.execute("SELECT parent_id FROM child").fetchall() == []
