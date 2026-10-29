from __future__ import annotations

import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "android-signed-release.yml"


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
    assert './gradlew bundleRelease "-PAIXION_API_BASE_URL=${AIXION_API_BASE_URL}"' in workflow
