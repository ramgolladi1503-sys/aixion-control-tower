from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = REPOSITORY_ROOT / "scripts" / "validate_android_release_url.py"
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "android-signed-release.yml"


def test_signed_release_url_validator_accepts_credential_free_https_urls() -> None:
    result = subprocess.run(
        [sys.executable, str(VALIDATOR)],
        cwd=REPOSITORY_ROOT,
        env={**os.environ, "AIXION_API_BASE_URL": "https://api.example.com/control-tower/"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_signed_release_url_validator_rejects_unsafe_or_malformed_urls() -> None:
    for value in (
        "",
        "http://api.example.com/",
        "https://api.example.com",
        "https://user:password@api.example.com/",
        "https://api.example.com/?token=secret/",
        "https://api.example.com/#secret/",
        "https://api.example.com:99999/",
        "https://bad_host.example.com/",
        "https://api.example.com/\n./sentinel",
    ):
        result = subprocess.run(
            [sys.executable, str(VALIDATOR)],
            cwd=REPOSITORY_ROOT,
            env={**os.environ, "AIXION_API_BASE_URL": value},
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 2, value
        assert "Release API base URL" in result.stderr
        if value:
            assert value not in result.stdout + result.stderr


def test_signed_release_workflow_does_not_interpolate_dispatch_inputs_into_shell() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    run_blocks: list[str] = []
    lines = workflow.splitlines()
    index = 0
    while index < len(lines):
        match = re.match(r"^(\s*)run:\s*(.*)$", lines[index])
        if match is None:
            index += 1
            continue
        run_indent = len(match.group(1))
        block = [match.group(2)] if match.group(2) else []
        index += 1
        while index < len(lines):
            line = lines[index]
            if line.strip() and len(line) - len(line.lstrip()) <= run_indent:
                break
            block.append(line)
            index += 1
        run_blocks.append("\n".join(block))

    assert run_blocks
    assert all("${{ inputs." not in block for block in run_blocks)
    assert "AIXION_API_BASE_URL: ${{ inputs.api_base_url }}" in workflow
    assert "python3 ../../scripts/validate_android_release_url.py" in workflow
    assert './gradlew bundleRelease "-PAIXION_API_BASE_URL=${AIXION_API_BASE_URL}"' in workflow
