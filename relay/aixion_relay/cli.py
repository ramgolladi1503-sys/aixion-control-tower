from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from .adapters import discover_default_adapters
from .adapters.antigravity import antigravity_tool_action
from .adapters.openclaw import openclaw_tool_action
from .client import AixionRelayClient, register_relay_host
from .config import (
    RelayConfig,
    default_config_path,
    default_registration_payload,
    load_config,
    save_config,
)
from .contracts import ActionProposal, PolicyDecision
from .runtime import UniversalRelayRuntime
from .secrets import SecretStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aixion-relay",
        description="Outbound-only universal agent relay for Aixion Control Tower.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Configuration path (default: {default_config_path()})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Register this machine with Aixion.")
    init.add_argument("--api-base-url", required=True)
    init.add_argument(
        "--owner-token",
        default=os.getenv("AIXION_OWNER_TOKEN", ""),
        help="Short-lived Aixion OWNER session token.",
    )
    init.add_argument("--name", required=True)
    init.add_argument("--workspace-root", action="append", required=True)
    init.add_argument("--repository", action="append", default=[])
    init.add_argument("--worker-id", default="aixion-local-relay")

    subparsers.add_parser("run", help="Run the relay until stopped.")
    subparsers.add_parser("once", help="Process at most one queued command.")
    subparsers.add_parser("doctor", help="Validate configuration and provider adapters.")

    hook = subparsers.add_parser(
        "hook",
        help="Resolve a provider tool hook through the Aixion trust plane.",
    )
    hook.add_argument("provider", choices=["antigravity", "openclaw", "generic"])
    hook.add_argument(
        "--session-id",
        default=os.getenv("AIXION_RELAY_SESSION_ID", ""),
    )
    return parser


async def _init(args: argparse.Namespace) -> int:
    if not args.owner_token:
        raise RuntimeError("--owner-token or AIXION_OWNER_TOKEN is required.")
    registry = discover_default_adapters()
    registration = default_registration_payload(
        name=args.name,
        workspace_roots=args.workspace_root,
        adapters=registry.manifests(),
        repositories=args.repository,
    )
    response = await register_relay_host(
        base_url=args.api_base_url,
        owner_access_token=args.owner_token,
        registration_payload=registration,
    )
    relay = response["relay"]
    relay_id = str(relay["id"])
    secret_name = f"relay-token:{relay_id}"
    SecretStore().set(secret_name, str(response["relay_token"]))
    config = RelayConfig(
        api_base_url=args.api_base_url,
        relay_id=relay_id,
        relay_token_secret_name=secret_name,
        worker_id=args.worker_id,
        workspace_roots=args.workspace_root,
        adapters=registry.manifests(),
        metadata={"registered_name": args.name},
    )
    path = save_config(config, args.config)
    await registry.close()
    print(json.dumps({"relay_id": relay_id, "config_path": str(path)}, indent=2))
    return 0


def _build_runtime(args: argparse.Namespace):
    config = load_config(args.config)
    relay_token = SecretStore().get(config.relay_token_secret_name)
    registry = discover_default_adapters()
    client = AixionRelayClient(
        base_url=config.api_base_url,
        relay_id=config.relay_id,
        relay_token=relay_token,
        timeout_seconds=config.request_timeout_seconds,
    )
    runtime = UniversalRelayRuntime(
        config=config,
        client=client,
        registry=registry,
    )
    return config, registry, runtime


async def _run(args: argparse.Namespace, *, once: bool) -> int:
    _, registry, runtime = _build_runtime(args)
    if once:
        try:
            await runtime.client.heartbeat(
                version="0.1.0",
                adapters=registry.manifests(),
                active_session_count=0,
            )
            await runtime.run_once()
            await asyncio.sleep(0)
        finally:
            await runtime.shutdown()
        return 0
    await runtime.run_forever()
    return 0


async def _doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    registry = discover_default_adapters()
    results = {
        "config": "ok",
        "relay_id": config.relay_id,
        "api_base_url": config.api_base_url,
        "workspace_roots": [],
        "adapters": [],
        "token": "missing",
    }
    for root in config.workspace_roots:
        path = Path(root)
        results["workspace_roots"].append(
            {"path": str(path), "exists": path.is_dir(), "writable": os.access(path, os.W_OK)}
        )
    results["adapters"] = [
        manifest.model_dump(mode="json")
        for manifest in registry.manifests()
    ]
    token = SecretStore().get(config.relay_token_secret_name)
    results["token"] = "configured" if token else "missing"
    client = AixionRelayClient(
        base_url=config.api_base_url,
        relay_id=config.relay_id,
        relay_token=token,
        timeout_seconds=config.request_timeout_seconds,
    )
    try:
        await client.heartbeat(
            version="0.1.0",
            adapters=registry.manifests(),
            active_session_count=0,
            metadata={"doctor": True},
        )
        results["api"] = "reachable"
    finally:
        await client.close()
        await registry.close()
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


def _hook_action(provider: str, payload: dict[str, Any]) -> ActionProposal:
    if provider == "antigravity":
        return antigravity_tool_action(payload)
    if provider == "openclaw":
        return openclaw_tool_action(payload)
    raw_action = payload.get("action") or payload
    return ActionProposal.model_validate(raw_action)


def _hook_output(provider: str, decision: PolicyDecision, reasons: list[str]) -> dict[str, Any]:
    allowed = decision == PolicyDecision.ALLOW
    reason = "; ".join(reasons)
    if provider == "antigravity":
        return {
            "decision": "allow" if allowed else "deny",
            "reason": reason,
        }
    if provider == "openclaw":
        return {
            "allow": allowed,
            "reason": reason,
        }
    return {
        "decision": decision.value,
        "reasons": reasons,
    }


async def _hook(args: argparse.Namespace) -> int:
    if not args.session_id:
        raise RuntimeError(
            "--session-id or AIXION_RELAY_SESSION_ID is required for provider hooks."
        )
    raw = sys.stdin.read()
    if not raw.strip():
        raise RuntimeError("Provider hook input is empty.")
    payload = json.loads(raw)
    proposal = _hook_action(args.provider, payload)
    config = load_config(args.config)
    token = SecretStore().get(config.relay_token_secret_name)
    async with AixionRelayClient(
        base_url=config.api_base_url,
        relay_id=config.relay_id,
        relay_token=token,
        timeout_seconds=config.request_timeout_seconds,
    ) as client:
        decision = await client.propose_action(args.session_id, proposal)
        while decision.decision == PolicyDecision.REQUIRE_APPROVAL:
            await asyncio.sleep(1)
            decision = await client.get_action_status(
                args.session_id,
                decision.action_id,
            )
    print(json.dumps(_hook_output(args.provider, decision.decision, decision.reasons)))
    return 0 if decision.decision == PolicyDecision.ALLOW else 2


async def _async_main(args: argparse.Namespace) -> int:
    if args.command == "init":
        return await _init(args)
    if args.command == "run":
        return await _run(args, once=False)
    if args.command == "once":
        return await _run(args, once=True)
    if args.command == "doctor":
        return await _doctor(args)
    if args.command == "hook":
        return await _hook(args)
    raise RuntimeError(f"Unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        return 130
    except Exception as error:  # noqa: BLE001 - CLI must return a clear non-zero result.
        print(f"aixion-relay: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
