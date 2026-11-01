from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
IMPORTER = REPOSITORY_ROOT / "backend" / "scripts" / "import_approval_slice_to_postgres.py"


def test_importer_help_runs_from_repository_root_without_pythonpath() -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(IMPORTER), "--help"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert "--source SOURCE" in result.stdout
    assert "--confirm-source-snapshot" in result.stdout
