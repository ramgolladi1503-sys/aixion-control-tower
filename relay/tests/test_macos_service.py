from __future__ import annotations

import os
import plistlib
import stat
from pathlib import Path

from aixion_relay.macos_service import (
    SERVICE_LABEL,
    _atomic_write_plist,
    service_definition,
)


def test_launch_agent_is_background_only_and_contains_no_secret(tmp_path: Path) -> None:
    executable = tmp_path / "aixion-relay"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    config = tmp_path / "relay.json"
    config.write_text('{"relay_id":"relay_test"}\n', encoding="utf-8")
    logs = tmp_path / "logs"

    payload = service_definition(
        executable=executable,
        config_path=config,
        log_directory=logs,
    )

    assert payload["Label"] == SERVICE_LABEL
    assert payload["ProgramArguments"] == [
        str(executable),
        "--config",
        str(config),
        "run",
    ]
    assert payload["ProcessType"] == "Background"
    assert payload["RunAtLoad"] is True
    assert "token" not in str(payload).lower()
    assert "owner" not in str(payload).lower()
    assert "window" not in str(payload).lower()


def test_atomic_plist_is_private_and_valid(tmp_path: Path) -> None:
    destination = tmp_path / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    payload = {
        "Label": SERVICE_LABEL,
        "ProgramArguments": ["/usr/local/bin/aixion-relay", "run"],
    }

    _atomic_write_plist(destination, payload)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    with destination.open("rb") as handle:
        assert plistlib.load(handle) == payload
    assert not any(path.suffix == ".tmp" for path in destination.parent.iterdir())


def test_atomic_plist_refuses_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.plist"
    target.write_text("do not replace", encoding="utf-8")
    destination = tmp_path / "service.plist"
    os.symlink(target, destination)

    try:
        _atomic_write_plist(destination, {"Label": SERVICE_LABEL})
    except RuntimeError as error:
        assert "unsafe LaunchAgent path" in str(error)
    else:
        raise AssertionError("Expected unsafe symlink to be rejected")

    assert target.read_text(encoding="utf-8") == "do not replace"
