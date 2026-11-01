from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

from app.agent_worker_container_validation import (
    ContainerValidationExecutor,
    ContainerValidationPolicy,
    DockerContainerRuntime,
)
from app.agent_worker_validation_runner import CommandExecutionResult


class UnavailableRuntime:
    def available(self) -> bool:
        return False

    def run_validation(self, *, command: str, cwd: Path, policy: ContainerValidationPolicy, timeout_seconds: int) -> CommandExecutionResult:
        raise AssertionError("run_validation should not be called when runtime is unavailable")


class CapturingRuntime:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def available(self) -> bool:
        return True

    def run_validation(self, *, command: str, cwd: Path, policy: ContainerValidationPolicy, timeout_seconds: int) -> CommandExecutionResult:
        self.calls.append({"command": command, "cwd": cwd, "policy": policy, "timeout_seconds": timeout_seconds})
        return CommandExecutionResult(command=command, exit_code=0, output_summary="container passed", duration_ms=11)


def test_container_validation_fails_closed_when_runtime_unavailable(tmp_path: Path) -> None:
    executor = ContainerValidationExecutor(cwd=tmp_path, timeout_seconds=30, runtime=UnavailableRuntime())

    result = executor("python -m pytest")

    assert result.passed is False
    assert result.exit_code == 127
    assert result.output_summary == "Container runtime unavailable; validation failed closed."
    assert result.allowed_prefix == "python -m pytest"


def test_container_validation_fails_closed_when_workspace_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    executor = ContainerValidationExecutor(cwd=missing, timeout_seconds=30, runtime=UnavailableRuntime())

    result = executor("python -m pytest")

    assert result.passed is False
    assert result.exit_code == 127
    assert "workspace does not exist" in result.output_summary


def test_container_validation_delegates_to_runtime_with_policy(tmp_path: Path) -> None:
    runtime = CapturingRuntime()
    policy = ContainerValidationPolicy(
        image="python:3.12-slim@sha256:" + "a" * 64,
        network_disabled=True,
        read_only_rootfs=True,
    )
    executor = ContainerValidationExecutor(cwd=tmp_path, timeout_seconds=45, policy=policy, runtime=runtime)

    result = executor("python -m pytest")

    assert result.passed is True
    assert result.output_summary == "container passed"
    assert runtime.calls == [{"command": "python -m pytest", "cwd": tmp_path, "policy": policy, "timeout_seconds": 45}]


def test_container_validation_policy_defaults_are_locked_down() -> None:
    policy = ContainerValidationPolicy()

    assert policy.runtime == "docker"
    assert policy.workspace_mount_mode == "ro"
    assert policy.network_disabled is True
    assert policy.read_only_rootfs is True
    assert policy.tmpfs_tmp is True
    assert policy.fail_closed_on_runtime_unavailable is True
    assert policy.memory_limit == "512m"
    assert policy.cpu_limit == "1.0"
    assert policy.pids_limit == 128
    assert policy.user == "65532:65532"


def _run_fake_docker(
    monkeypatch,
    *,
    tmp_path: Path,
    program: str,
    timeout_seconds: int = 10,
) -> CommandExecutionResult:
    real_popen = subprocess.Popen

    def fake_popen(_docker_args, **kwargs):
        return real_popen([sys.executable, "-c", program], **kwargs)

    monkeypatch.setattr("app.agent_worker_container_validation.subprocess.Popen", fake_popen)
    return DockerContainerRuntime().run_validation(
        command="python -m pytest",
        cwd=tmp_path,
        policy=ContainerValidationPolicy(image="python:3.12-slim@sha256:" + "a" * 64),
        timeout_seconds=timeout_seconds,
    )


def test_docker_runtime_hashes_stdout_and_stderr_separately(monkeypatch, tmp_path: Path) -> None:
    result = _run_fake_docker(
        monkeypatch,
        tmp_path=tmp_path,
        program="import os; os.write(1, b'stdout'); os.write(2, b'stderr')",
    )

    assert result.exit_code == 0
    assert result.stream_evidence_complete is True
    assert result.output_limit_exceeded is False
    assert (result.stdout_sha256, result.stdout_bytes) == (
        hashlib.sha256(b"stdout").hexdigest(), 6,
    )
    assert (result.stderr_sha256, result.stderr_bytes) == (
        hashlib.sha256(b"stderr").hexdigest(), 6,
    )


def test_docker_runtime_marks_timeout_with_complete_separate_stream_evidence(
    monkeypatch, tmp_path: Path,
) -> None:
    result = _run_fake_docker(
        monkeypatch,
        tmp_path=tmp_path,
        program="import os, time; os.write(1, b'before-timeout'); time.sleep(5)",
        timeout_seconds=1,
    )

    assert result.timed_out is True
    assert result.exit_code == 124
    assert result.stream_evidence_complete is True
    assert result.stdout_sha256 == hashlib.sha256(b"before-timeout").hexdigest()
    assert result.stdout_bytes == len(b"before-timeout")
    assert result.stderr_sha256 == hashlib.sha256(b"").hexdigest()
    assert result.stderr_bytes == 0


def test_docker_runtime_rejects_mutable_image_tag(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="pinned by immutable SHA-256 digest"):
        DockerContainerRuntime().run_validation(
            command="python -m pytest",
            cwd=tmp_path,
            policy=ContainerValidationPolicy(image="python:3.12-slim"),
            timeout_seconds=10,
        )


def test_docker_runtime_keeps_bounded_stream_hashes_when_output_exceeds_limit(
    monkeypatch, tmp_path: Path,
) -> None:
    result = _run_fake_docker(
        monkeypatch,
        tmp_path=tmp_path,
        program=(
            "import os; data=b'x'*65536; "
            "exec('while True: os.write(1, data)')"
        ),
    )

    expected = b"x" * result.stdout_bytes
    assert result.stream_evidence_complete is False
    assert result.output_limit_exceeded is True
    assert 1_048_576 < result.stdout_bytes < 4 * 1024 * 1024
    assert result.stdout_sha256 == hashlib.sha256(expected).hexdigest()
    assert result.stderr_sha256 == hashlib.sha256(b"").hexdigest()
    assert result.stderr_bytes == 0
