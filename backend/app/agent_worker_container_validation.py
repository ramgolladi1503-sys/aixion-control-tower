from __future__ import annotations

import hashlib
import os
import re
import selectors
import signal
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from .agent_worker_validation_plan import allowed_prefix_for_command
from .agent_worker_validation_runner import CommandExecutionResult, _summarize_output


@dataclass(frozen=True)
class ContainerValidationPolicy:
    policy_version: str = "aixion-container-validation-policy-v1"
    runtime: str = "docker"
    image: str = field(default_factory=lambda: os.getenv("AIXION_VALIDATION_CONTAINER_IMAGE", "").strip())
    workdir: str = "/workspace"
    workspace_mount_mode: str = "ro"
    network_disabled: bool = True
    read_only_rootfs: bool = True
    tmpfs_tmp: bool = True
    fail_closed_on_runtime_unavailable: bool = True
    max_output_chars: int = 4000
    memory_limit: str = "512m"
    cpu_limit: str = "1.0"
    pids_limit: int = 128
    user: str = "65532:65532"


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
        if re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", policy.image) is None:
            raise ValueError("Container image must be pinned by immutable SHA-256 digest")
        docker_args = [
            "docker",
            "run",
            "--rm",
            "--workdir",
            policy.workdir,
            "--volume",
            f"{cwd.resolve()}:{policy.workdir}:{policy.workspace_mount_mode}",
            "--memory",
            policy.memory_limit,
            "--cpus",
            policy.cpu_limit,
            "--pids-limit",
            str(policy.pids_limit),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            policy.user,
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
        digests = {"stdout": hashlib.sha256(), "stderr": hashlib.sha256()}
        byte_counts = {"stdout": 0, "stderr": 0}
        tails = {"stdout": bytearray(), "stderr": bytearray()}
        output_limit = 1024 * 1024
        output_limit_exceeded = False
        output_limit_termination_at: float | None = None
        timed_out = False
        exit_code = 127
        selector = selectors.DefaultSelector()
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                docker_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            if process.stdout is None or process.stderr is None:
                raise RuntimeError("Container output pipes were not created")
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            deadline = started + timeout_seconds
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if (
                    output_limit_exceeded
                    and process.poll() is None
                    and output_limit_termination_at is not None
                    and time.monotonic() - output_limit_termination_at >= 1.0
                ):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if remaining <= 0 and process.poll() is None:
                    timed_out = True
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    deadline = float("inf")
                for key, _mask in selector.select(timeout=min(0.1, max(0.0, remaining))):
                    stream = key.data
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    digests[stream].update(chunk)
                    byte_counts[stream] += len(chunk)
                    if byte_counts[stream] > output_limit and not output_limit_exceeded:
                        output_limit_exceeded = True
                        output_limit_termination_at = time.monotonic()
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    tails[stream].extend(chunk)
                    if len(tails[stream]) > policy.max_output_chars * 4:
                        del tails[stream][:-policy.max_output_chars * 4]
            exit_code = process.wait()
            duration_ms = int((time.monotonic() - started) * 1000)
            stdout = bytes(tails["stdout"]).decode("utf-8", errors="replace")
            stderr = bytes(tails["stderr"]).decode("utf-8", errors="replace")
            combined_output = "\n".join(part for part in [stdout, stderr] if part)
            summary, truncated, output_chars = _summarize_output(combined_output)
            return CommandExecutionResult(
                command=command,
                exit_code=124 if timed_out else exit_code,
                timed_out=timed_out,
                output_summary=summary,
                duration_ms=duration_ms,
                output_truncated=truncated,
                output_chars=output_chars,
                allowed_prefix=allowed_prefix_for_command(command),
                stdout_sha256=digests["stdout"].hexdigest(),
                stdout_bytes=byte_counts["stdout"],
                stderr_sha256=digests["stderr"].hexdigest(),
                stderr_bytes=byte_counts["stderr"],
                stream_evidence_complete=not output_limit_exceeded,
                output_limit_exceeded=output_limit_exceeded,
            )
        except Exception:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            duration_ms = int((time.monotonic() - started) * 1000)
            message = "Container runtime failed before bounded stream evidence completed."
            summary, truncated, output_chars = _summarize_output(message)
            return CommandExecutionResult(
                command=command,
                exit_code=127,
                timed_out=False,
                output_summary=summary,
                duration_ms=duration_ms,
                output_truncated=truncated,
                output_chars=output_chars,
                allowed_prefix=allowed_prefix_for_command(command),
                stream_evidence_complete=False,
            )
        finally:
            selector.close()


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
            stdout_sha256=hashlib.sha256(b"").hexdigest(),
            stdout_bytes=0,
            stderr_sha256=hashlib.sha256(reason.encode("utf-8")).hexdigest(),
            stderr_bytes=len(reason.encode("utf-8")),
            stream_evidence_complete=True,
        )
