from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).with_name("store_agent_token.py")
SPEC = importlib.util.spec_from_file_location("aixion_store_antigravity_agent_token", MODULE_PATH)
assert SPEC and SPEC.loader
store = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(store)


class _TTY:
    def isatty(self) -> bool:
        return True


def test_store_uses_secure_prompt_and_never_overwrites(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store.sys, "stdin", _TTY())
    monkeypatch.setattr(store.sys, "stderr", _TTY())
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    store.store_token("agent_test-1", platform="darwin", run=fake_run)

    assert calls == [(
        [
            "/usr/bin/security", "add-generic-password",
            "-s", "com.aixion.controltower.agent", "-a", "agent_test-1", "-w",
        ],
        {"check": False, "timeout": 300},
    )]
    assert "-U" not in calls[0][0]


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_store_refuses_non_macos_before_invoking_keychain(platform: str) -> None:
    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Keychain command must not run outside macOS")

    with pytest.raises(ValueError, match="only on macOS"):
        store.store_token("agent_test", platform=platform, run=fail_if_called)


def test_store_rejects_invalid_agent_id_before_invoking_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store.sys, "stdin", _TTY())
    monkeypatch.setattr(store.sys, "stderr", _TTY())

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Invalid identifier must not reach Keychain")

    with pytest.raises(ValueError, match="unsupported characters"):
        store.store_token("agent\n-w", platform="darwin", run=fail_if_called)


def test_store_requires_interactive_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(store.sys, "stderr", _TTY())

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Noninteractive calls must not invoke Keychain")

    with pytest.raises(ValueError, match="interactive terminal"):
        store.store_token("agent_test", platform="darwin", run=fail_if_called)


def test_nonzero_keychain_result_fails_without_printing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store.sys, "stdin", _TTY())
    monkeypatch.setattr(store.sys, "stderr", _TTY())

    def fake_run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1)

    with pytest.raises(RuntimeError, match="not stored") as error:
        store.store_token("agent_test", platform="darwin", run=fake_run)
    assert "token" not in str(error.value).lower()
