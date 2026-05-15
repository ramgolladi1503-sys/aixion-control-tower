from __future__ import annotations

from pathlib import Path

from app.agent_worker_container_validation import ContainerValidationExecutor, ContainerValidationPolicy
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
    policy = ContainerValidationPolicy(image="python:3.12-slim", network_disabled=True, read_only_rootfs=True)
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
