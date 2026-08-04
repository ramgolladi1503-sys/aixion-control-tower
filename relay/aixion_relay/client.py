from __future__ import annotations

from typing import Any

import httpx

from .contracts import (
    ActionDecision,
    ActionProposal,
    AdapterManifest,
    NormalizedEvent,
    PolicyDecision,
    RelayCommand,
)


class RelayApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AixionRelayClient:
    def __init__(
        self,
        *,
        base_url: str,
        relay_id: str,
        relay_token: str,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.relay_id = relay_id
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"X-Aixion-Relay-Token": relay_token},
            timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds)),
            transport=transport,
        )

    async def __aenter__(self) -> AixionRelayClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise RelayApiError(f"Aixion relay API request failed: {error}") from error
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise RelayApiError(
                f"Aixion relay API returned {response.status_code}: {detail}",
                status_code=response.status_code,
            )
        if not response.content:
            return None
        return response.json()

    async def heartbeat(
        self,
        *,
        version: str,
        adapters: list[AdapterManifest],
        active_session_count: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._request(
            "POST",
            f"connectors/relay-hosts/{self.relay_id}/heartbeat",
            json={
                "relay_version": version,
                "adapters": [adapter.model_dump(mode="json") for adapter in adapters],
                "active_session_count": active_session_count,
                "metadata": metadata or {},
            },
        )

    async def claim_command(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> RelayCommand | None:
        payload = await self._request(
            "POST",
            f"connectors/relay-hosts/{self.relay_id}/commands/claim",
            json={"worker_id": worker_id, "lease_seconds": lease_seconds},
        )
        command = payload.get("command") if payload else None
        return RelayCommand.model_validate(command) if command else None

    async def acknowledge_command(
        self,
        command: RelayCommand,
        *,
        worker_id: str,
    ) -> None:
        await self._request(
            "POST",
            (
                f"connectors/relay-hosts/{self.relay_id}/commands/"
                f"{command.id}/ack"
            ),
            json={
                "worker_id": worker_id,
                "lease_token": self._lease_token(command),
            },
        )

    async def heartbeat_command(
        self,
        command: RelayCommand,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> None:
        await self._request(
            "POST",
            (
                f"connectors/relay-hosts/{self.relay_id}/commands/"
                f"{command.id}/heartbeat"
            ),
            json={
                "worker_id": worker_id,
                "lease_token": self._lease_token(command),
                "lease_seconds": lease_seconds,
            },
        )

    async def complete_command(
        self,
        command: RelayCommand,
        *,
        worker_id: str,
        success: bool,
        remote_session_id: str | None = None,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        await self._request(
            "POST",
            (
                f"connectors/relay-hosts/{self.relay_id}/commands/"
                f"{command.id}/complete"
            ),
            json={
                "worker_id": worker_id,
                "lease_token": self._lease_token(command),
                "success": success,
                "remote_session_id": remote_session_id,
                "error": error,
                "result": result or {},
            },
        )

    async def post_event(
        self,
        session_id: str,
        event_id: str,
        sequence: int,
        event: NormalizedEvent,
    ) -> None:
        await self._request(
            "POST",
            (
                f"connectors/relay-hosts/{self.relay_id}/sessions/"
                f"{session_id}/events"
            ),
            json={
                "event_id": event_id,
                "sequence": sequence,
                **event.model_dump(mode="json"),
            },
        )

    async def propose_action(
        self,
        session_id: str,
        proposal: ActionProposal,
    ) -> ActionDecision:
        payload = await self._request(
            "POST",
            (
                f"connectors/relay-hosts/{self.relay_id}/sessions/"
                f"{session_id}/actions"
            ),
            json={
                "action": {
                    "action_type": proposal.action_type.value,
                    "lease_id": proposal.lease_id,
                    "run_id": proposal.run_id,
                    "task_id": proposal.task_id,
                    "project_id": proposal.project_id,
                    "repository": proposal.repository,
                    "branch": proposal.branch,
                    "paths": proposal.paths,
                    "command": proposal.command,
                    "network_domains": proposal.network_domains,
                    "estimated_runtime_seconds": proposal.estimated_runtime_seconds,
                    "estimated_cost_usd": proposal.estimated_cost_usd,
                    "retry_number": proposal.retry_number,
                    "metadata": proposal.metadata,
                }
            },
        )
        return self._parse_decision(payload)

    async def get_action_status(
        self,
        session_id: str,
        action_id: str,
    ) -> ActionDecision:
        payload = await self._request(
            "GET",
            (
                f"connectors/relay-hosts/{self.relay_id}/sessions/"
                f"{session_id}/actions/{action_id}"
            ),
        )
        return self._parse_decision(payload)

    @staticmethod
    def _parse_decision(payload: dict[str, Any]) -> ActionDecision:
        action = payload["action"]
        decision = payload["decision"]
        return ActionDecision(
            action_id=action["id"],
            decision=PolicyDecision(decision["decision"]),
            reasons=decision.get("reasons") or [],
            obligations=decision.get("obligations") or [],
        )

    @staticmethod
    def _lease_token(command: RelayCommand) -> str:
        if not command.lease_token:
            raise RelayApiError("Claimed relay command is missing its lease token.")
        return command.lease_token


async def register_relay_host(
    *,
    base_url: str,
    owner_access_token: str,
    registration_payload: dict[str, Any],
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {owner_access_token}"}
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/") + "/",
        headers=headers,
        timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds)),
    ) as client:
        response = await client.post(
            "connectors/relay-hosts/register",
            json=registration_payload,
        )
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise RelayApiError(
                f"Relay registration failed with {response.status_code}: {detail}",
                status_code=response.status_code,
            )
        return response.json()
