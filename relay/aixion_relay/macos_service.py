from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .config import default_config_path

SERVICE_LABEL = "com.aixion.agent-relay"


class MacOSServiceError(RuntimeError):
    pass


def default_launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def default_log_directory() -> Path:
    return Path.home() / "Library" / "Logs" / "Aixion"


def service_definition(
    *,
    executable: Path,
    config_path: Path,
    log_directory: Path,
) -> dict[str, Any]:
    """Return a background-only LaunchAgent definition.

    The relay token is deliberately absent. The process reads only the non-secret
    config path and retrieves the relay credential from the operating-system secret
    store at runtime.
    """

    return {
        "Label": SERVICE_LABEL,
        "ProgramArguments": [
            str(executable),
            "--config",
            str(config_path),
            "run",
        ],
        "RunAtLoad": True,
        "KeepAlive": {
            "SuccessfulExit": False,
            "NetworkState": True,
        },
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_directory / "relay.out.log"),
        "StandardErrorPath": str(log_directory / "relay.err.log"),
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
        },
    }


def _atomic_write_plist(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise MacOSServiceError(f"Refusing unsafe LaunchAgent path: {path}")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(payload, handle, fmt=plistlib.FMT_XML, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _launchctl() -> str:
    executable = shutil.which("launchctl")
    if executable is None:
        raise MacOSServiceError("launchctl was not found.")
    return executable


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def _run_launchctl(*arguments: str, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [_launchctl(), *arguments],
        text=True,
        capture_output=True,
        check=False,
        shell=False,
    )
    if completed.returncode != 0 and not allow_failure:
        detail = (completed.stderr or completed.stdout or "launchctl failed").strip()
        raise MacOSServiceError(detail[:2000])
    return completed


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise MacOSServiceError("The Aixion LaunchAgent installer is available on macOS only.")


def install_service(
    *,
    executable: Path | None = None,
    config_path: Path | None = None,
    launch_agent_path: Path | None = None,
) -> dict[str, str]:
    _require_macos()
    resolved_executable = (executable or Path(sys.argv[0])).expanduser().resolve()
    resolved_config = (config_path or default_config_path()).expanduser().resolve()
    resolved_plist = (launch_agent_path or default_launch_agent_path()).expanduser().resolve()
    log_directory = default_log_directory().expanduser().resolve()

    if not resolved_executable.is_file() or not os.access(resolved_executable, os.X_OK):
        raise MacOSServiceError(f"Relay executable is not executable: {resolved_executable}")
    if not resolved_config.is_file() or resolved_config.is_symlink():
        raise MacOSServiceError(f"Relay configuration is unavailable or unsafe: {resolved_config}")

    log_directory.mkdir(parents=True, exist_ok=True)
    _atomic_write_plist(
        resolved_plist,
        service_definition(
            executable=resolved_executable,
            config_path=resolved_config,
            log_directory=log_directory,
        ),
    )

    domain = _gui_domain()
    _run_launchctl("bootout", domain, str(resolved_plist), allow_failure=True)
    _run_launchctl("bootstrap", domain, str(resolved_plist))
    _run_launchctl("enable", f"{domain}/{SERVICE_LABEL}")
    _run_launchctl("kickstart", "-k", f"{domain}/{SERVICE_LABEL}")
    return {
        "status": "installed",
        "label": SERVICE_LABEL,
        "plist": str(resolved_plist),
        "config": str(resolved_config),
        "stdout_log": str(log_directory / "relay.out.log"),
        "stderr_log": str(log_directory / "relay.err.log"),
        "visible_agent_window": "false",
    }


def uninstall_service(
    *,
    launch_agent_path: Path | None = None,
) -> dict[str, str]:
    _require_macos()
    resolved_plist = (launch_agent_path or default_launch_agent_path()).expanduser().resolve()
    _run_launchctl("bootout", _gui_domain(), str(resolved_plist), allow_failure=True)
    if resolved_plist.exists():
        if resolved_plist.is_symlink() or not resolved_plist.is_file():
            raise MacOSServiceError(f"Refusing unsafe LaunchAgent path: {resolved_plist}")
        resolved_plist.unlink()
    return {
        "status": "uninstalled",
        "label": SERVICE_LABEL,
        "plist": str(resolved_plist),
    }


def service_status() -> dict[str, str]:
    _require_macos()
    target = f"{_gui_domain()}/{SERVICE_LABEL}"
    completed = _run_launchctl("print", target, allow_failure=True)
    return {
        "status": "running" if completed.returncode == 0 else "not_loaded",
        "label": SERVICE_LABEL,
        "plist": str(default_launch_agent_path()),
    }
