#!/usr/bin/env python3
"""Store an Antigravity connector token in macOS Keychain via its secure prompt.

The token is entered by the ``security`` utility at its hidden prompt. It is
never accepted as a command-line argument, environment variable, or stdin data.
Existing Keychain items are not replaced.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

KEYCHAIN_SERVICE = "com.aixion.controltower.agent"
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")


def store_token(agent_id: str, *, platform: str | None = None, run=subprocess.run) -> None:
    """Prompt securely and add one Keychain item without replacing an existing one."""
    actual_platform = sys.platform if platform is None else platform
    if actual_platform != "darwin":
        raise ValueError("macOS Keychain provisioning is available only on macOS")
    if not isinstance(agent_id, str) or not AGENT_ID_RE.fullmatch(agent_id):
        raise ValueError("Agent ID is empty or contains unsupported characters")
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ValueError("Run this command from an interactive terminal for the secure Keychain prompt")

    # Apple's `security` prompts for the password when -w is the final option.
    # Omitting -U makes duplicate items fail instead of replacing credentials.
    command = [
        "/usr/bin/security", "add-generic-password",
        "-s", KEYCHAIN_SERVICE,
        "-a", agent_id,
        "-w",
    ]
    try:
        result = run(command, check=False, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("macOS Keychain did not complete credential storage") from exc
    if result.returncode != 0:
        raise RuntimeError(
            "Credential was not stored. Keychain may have rejected an existing item or the prompt was cancelled."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", required=True, help="Registered Antigravity agent ID")
    args = parser.parse_args(argv)
    try:
        store_token(args.agent_id)
    except (ValueError, RuntimeError) as exc:
        print(f"Antigravity Keychain setup failed: {exc}", file=sys.stderr)
        return 2
    print("Antigravity connector token stored in macOS Keychain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
