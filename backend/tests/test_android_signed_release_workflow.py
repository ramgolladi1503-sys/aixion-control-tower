from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "android-signed-release.yml"
FIREBASE_CONFIG_INSTALLER = REPOSITORY_ROOT / "scripts" / "install_android_firebase_config.py"


def _firebase_config_secret(config: object) -> str:
    return base64.b64encode(json.dumps(config).encode("utf-8")).decode("ascii")


def _run_firebase_config_installer(
    secret: str,
    cwd: Path,
    *,
    project_id: str | None = "aixion-release",
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "ANDROID_GOOGLE_SERVICES_JSON_BASE64": secret,
    }
    if project_id is not None:
        environment["AIXION_FCM_PROJECT_ID"] = project_id
    else:
        environment.pop("AIXION_FCM_PROJECT_ID", None)
    return subprocess.run(
        [sys.executable, str(FIREBASE_CONFIG_INSTALLER)],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _valid_firebase_config() -> dict[str, object]:
    return {
        "project_info": {"project_id": "aixion-release"},
        "client": [
            {
                "client_info": {
                    "android_client_info": {"package_name": "com.aixion.controltower"}
                }
            }
        ],
    }


def test_firebase_config_installer_creates_private_release_config() -> None:
    config = _valid_firebase_config()
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        destination = root / "mobile/android/app/google-services.json"
        destination.parent.mkdir(parents=True)

        result = _run_firebase_config_installer(_firebase_config_secret(config), root)

        assert result.returncode == 0, result.stderr
        assert json.loads(destination.read_text(encoding="utf-8")) == config
        assert destination.stat().st_mode & 0o777 == 0o600


def test_firebase_config_installer_rejects_invalid_or_mismatched_config() -> None:
    invalid_secrets = (
        "not-base64!",
        _firebase_config_secret({"project_info": {"project_id": "aixion-release"}, "client": []}),
        _firebase_config_secret(
            {
                "project_info": {"project_id": "aixion-release"},
                "client": [
                    {
                        "client_info": {
                            "android_client_info": {"package_name": "com.aixion.controltower.debug"}
                        }
                    }
                ],
            }
        ),
        _firebase_config_secret(
            {
                **_valid_firebase_config(),
                "project_info": {"project_id": "different-firebase-project"},
            }
        ),
        _firebase_config_secret(
            {
                **_valid_firebase_config(),
                "client": _valid_firebase_config()["client"] * 2,
            }
        ),
    )
    for secret in invalid_secrets:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            destination = root / "mobile/android/app/google-services.json"
            destination.parent.mkdir(parents=True)

            result = _run_firebase_config_installer(secret, root)

            assert result.returncode != 0
            assert not destination.exists()
            assert secret not in result.stdout + result.stderr


def test_firebase_config_installer_requires_backend_project_id_variable() -> None:
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        destination = root / "mobile/android/app/google-services.json"
        destination.parent.mkdir(parents=True)

        result = _run_firebase_config_installer(
            _firebase_config_secret(_valid_firebase_config()),
            root,
            project_id=None,
        )

        assert result.returncode != 0
        assert "AIXION_FCM_PROJECT_ID is missing" in result.stderr
        assert not destination.exists()


def test_firebase_config_installer_refuses_to_overwrite_existing_file() -> None:
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        destination = root / "mobile/android/app/google-services.json"
        destination.parent.mkdir(parents=True)
        destination.write_text("owner file", encoding="utf-8")

        result = _run_firebase_config_installer(_firebase_config_secret(_valid_firebase_config()), root)

        assert result.returncode != 0
        assert destination.read_text(encoding="utf-8") == "owner file"
        assert "refusing to overwrite" in result.stderr


def test_signed_release_workflow_does_not_interpolate_dispatch_inputs_into_shell() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    installer = FIREBASE_CONFIG_INSTALLER.read_text(encoding="utf-8")
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
    assert "ANDROID_GOOGLE_SERVICES_JSON_BASE64: ${{ secrets.ANDROID_GOOGLE_SERVICES_JSON_BASE64 }}" in workflow
    assert "AIXION_FCM_PROJECT_ID: ${{ vars.AIXION_FCM_PROJECT_ID }}" in workflow
    assert "environment: android-release" in workflow
    assert "      version_code:" in workflow
    assert "      version_name:" in workflow
    assert "AIXION_VERSION_CODE: ${{ inputs.version_code }}" in workflow
    assert "AIXION_VERSION_NAME: ${{ inputs.version_name }}" in workflow
    assert "python3 scripts/install_android_firebase_config.py" in workflow
    assert "-PAIXION_FIREBASE_CONFIG_ENABLED=true" in workflow
    assert '"-PAIXION_API_BASE_URL=${AIXION_API_BASE_URL}"' in workflow
    assert '"-PAIXION_VERSION_CODE=${AIXION_VERSION_CODE}"' in workflow
    assert '"-PAIXION_VERSION_NAME=${AIXION_VERSION_NAME}"' in workflow
    assert 'RELEASE_PACKAGE_NAME = "com.aixion.controltower"' in installer
    assert 'if: always() && steps.firebase_config.outputs.created == \'true\'' in workflow
    assert 'echo "created=true" >> "$GITHUB_OUTPUT"' in workflow
    assert "run: rm -f mobile/android/release.keystore" in workflow
    assert "run: rm -f mobile/android/app/google-services.json" in workflow
