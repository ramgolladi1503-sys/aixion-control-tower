from __future__ import annotations

from pathlib import Path


BACKEND_ROOT = Path(__file__).parents[1]


def _candidate_is_reopened(path: str, reopened_rules: list[str]) -> bool:
    return any(
        (rule == path)
        or (rule.endswith("/**") and path.startswith(rule[:-3]))
        or (rule.endswith("/") and path == rule[:-1])
        for rule in reopened_rules
    )


def _candidate_is_excluded_by_final_rules(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        "__pycache__" in path.split("/")
        or name.endswith((".pyc", ".pyo", ".pyd"))
        or name.startswith(".env")
        or name.endswith(".env")
        or ".env." in name
        or any(name.endswith(suffix) for suffix in (".sqlite", ".sqlite3", ".db"))
    )


def test_backend_docker_context_is_source_only_and_excludes_local_state() -> None:
    ignore_file = BACKEND_ROOT / ".dockerignore"
    rules = [
        line.strip()
        for line in ignore_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert rules == [
        "**",
        "!pyproject.toml",
        "!app/",
        "!app/**",
        "!scripts/",
        "!scripts/**",
        "**/__pycache__/",
        "**/*.py[cod]",
        "**/.env*",
        "**/*.env",
        "**/*.env.*",
        "**/*.sqlite*",
    ]

    # The allowlist only reopens package source and backend scripts. Local
    # credentials, environments, databases, tests, and caches stay excluded.
    reopened = [rule[1:] for rule in rules if rule.startswith("!")]
    assert reopened == ["pyproject.toml", "app/", "app/**", "scripts/", "scripts/**"]
    forbidden_paths = (
        ".env.production",
        ".venv/lib/python/site.py",
        "runtime/control-tower.sqlite3",
        "tests/test_secret_fixture.py",
        ".git/config",
        "app/__pycache__/main.cpython-314.pyc",
        "scripts/local.env",
        "app/runtime.sqlite3",
    )
    assert all(
        not _candidate_is_reopened(path, reopened)
        or _candidate_is_excluded_by_final_rules(path)
        for path in forbidden_paths
    )
