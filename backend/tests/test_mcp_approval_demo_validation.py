from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_mcp_approval_demo_validation_script_passes(tmp_path: Path) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    script_path = backend_root / "scripts" / "validate_mcp_approval_demo.py"
    env = os.environ.copy()
    env["AIXION_AUTH_ENABLED"] = "false"
    env["AIXION_DB_PATH"] = str(tmp_path / "mcp_approval_demo_validation.sqlite3")

    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=backend_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "MCP approval demo validation PASSED" in result.stdout
