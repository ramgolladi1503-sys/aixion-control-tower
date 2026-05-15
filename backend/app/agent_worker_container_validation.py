from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .agent_worker_validation_plan import allowed_prefix_for_command
from .agent_worker_validation_runner import CommandExecutionResult, _summarize_output


@dataclass(frozen=True)
class ContainerValidationPolicy:
    policy_version: str = "aixion-container-validation-policy-v1"
    runtime: str = "docker"
    image: str = "python:3.12-slim"
    workdir: str = "/workspace"
    workspace_mount_mode: str = "ro"
    network_disabled: bool = True
    read_only_rootfs: bool = True
    tmpfs_tmp: bool = True
    fail_closed_on_runtime_unavailable: bool = True
    max_output_chars: int = 4000


def container_validation_policy_metadata(policy: ContainerValidationPolicy | None = None) -> dict:
    return asdict(policy or ContainerValidationPolicy())


class ContainerRuntime(Protocol):
    def available(self) -> bool: ...

    def run_validation(
        self,
        *,
        command: str,
        cwd: Path,
        policy: ContainerValidationPolicy,
        timeout_seconds: int,
    ) -> CommandExecutionResult: ...


class DockerContainerRuntime:
    def available(self) -> bool:
        try:
            completed = subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                shell=False,
            )
            return completed.returncode == 0
        except Exception:
            return False

    def run_validation(
        self,
        *,
        command: str,
        cwd: Path,
        policy: ContainerValidationPolicy,
        timeout_seconds: int,
    ) -> CommandExecutionResult:
        docker_args = [
            "docker",
            "run",
            "--rm",
            "--workdir",
            policy.workdir,
            "--volume",
            f"{cwd.resolve()}:{policy.workdir}:{policy.workspace_mount_mode}",
        ]
        if policy.network_disabled:
            docker_args.extend(["--network", "none"])
        if policy.read_only_rootfs:
            docker_args.append("--read-only")
        if policy.tmpfs_tmp:
            docker_args.extend(["--tmpfs", "/tmp:rw,noexec,nosuid,size=128m"])
        docker_args.append(policy.image)
        docker_args.extend(shlex.split(command))

        started = time.monotonic()
        try:
            completed = subprocess.run(
                docker_args,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            combined_output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
            summary, truncated, output_chars = _summarize_output(combined_output)
            return CommandExecutionResult(
                command=command,
                exit_code=completed.returncode,
                timed_out=False,
                output_summary=summary,
                duration_ms=duration_ms,
                output_truncated=truncated,
                output_chars=output_chars,
                allowed_prefix=allowed_prefix_for_command(command),
            )
        except subprocess.TimeoutExpired as error:
            duration_ms = int((time.monotonic() - started) * 1000)
            output = "\n".join(
                part.decode("utf-8", errors="replace") if isinstance(part, bytes) else part
                for part in [error.stdout, error.stderr]
                if part
            )
            summary, truncated, output_chars = _summarize_output(output or f"Container validation timed out after {timeout_seconds}s.")
            return CommandExecutionResult(
                command=command,
                exit_code=124,
                timed_out=True,
                output_summary=summary,
                duration_ms=duration_ms,
                output_truncated=truncated,
                output_chars=output_chars,
                allowed_prefix=allowed_prefix_for_command(command),
            )


class ContainerValidationExecutor:
    def __init__(
        self,
        *,
        cwd: Path,
        timeout_seconds: int,
        policy: ContainerValidationPolicy | None = None,
        runtime: ContainerRuntime | None = None,
    ) -> None:
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.policy = policy or ContainerValidationPolicy()
        self.runtime = runtime or DockerContainerRuntime()
        self._available: bool | None = None

    def __call__(self, command: str) -> CommandExecutionResult:
        if not self.cwd.exists() or not self.cwd.is_dir():
            return self._fail_closed(command, f"Container validation workspace does not exist: {self.cwd}")
        if self._available is None:
            self._available = self.runtime.available()
        if not self._available:
            return self._fail_closed(command, "Container runtime unavailable; validation failed closed.")
        return self.runtime.run_validation(
            command=command,
            cwd=self.cwd,
            policy=self.policy,
            timeout_seconds=self.timeout_seconds,
        )

    def _fail_closed(self, command: str, reason: str) -> CommandExecutionResult:
        return CommandExecutionResult(
            command=command,
            exit_code=127,
            timed_out=False,
            output_summary=reason,
            duration_ms=0,
            output_truncated=False,
            output_chars=len(reason),
            allowed_prefix=allowed_prefix_for_command(command),
        )
