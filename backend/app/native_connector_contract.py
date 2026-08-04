from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class NativeConnectorMode(StrEnum):
    """How Aixion interacts with the provider runtime."""

    NATIVE_EXISTING_SESSION = "NATIVE_EXISTING_SESSION"
    AIXION_MANAGED_SESSION = "AIXION_MANAGED_SESSION"


class NativeConnectorSupport(StrEnum):
    """Provider support level for the declared connector mode."""

    PUBLIC_SUPPORTED = "PUBLIC_SUPPORTED"
    PARTNER_REQUIRED = "PARTNER_REQUIRED"
    FIRST_PARTY_ONLY = "FIRST_PARTY_ONLY"
    MANAGED_ONLY = "MANAGED_ONLY"
    UNVERIFIED = "UNVERIFIED"
    UNSUPPORTED = "UNSUPPORTED"


class NativeConnectorCapability(StrEnum):
    """Structured capabilities that a provider connector can prove."""

    DISCOVER_EXISTING_SESSIONS = "DISCOVER_EXISTING_SESSIONS"
    ATTACH_EXISTING_SESSION = "ATTACH_EXISTING_SESSION"
    STREAM_SESSION_STATE = "STREAM_SESSION_STATE"
    OBSERVE_APPROVAL_REQUESTS = "OBSERVE_APPROVAL_REQUESTS"
    RESOLVE_APPROVAL_REQUESTS = "RESOLVE_APPROVAL_REQUESTS"
    PRESERVE_PROVIDER_DECISIONS = "PRESERVE_PROVIDER_DECISIONS"
    CONTINUE_SAME_EXECUTION_CONTEXT = "CONTINUE_SAME_EXECUTION_CONTEXT"
    REVOKE_CONNECTION = "REVOKE_CONNECTION"
    STEER_SESSION = "STEER_SESSION"
    CANCEL_SESSION = "CANCEL_SESSION"
    START_MANAGED_RUNTIME = "START_MANAGED_RUNTIME"
    STRUCTURED_PROVIDER_EVENTS = "STRUCTURED_PROVIDER_EVENTS"


REQUIRED_NATIVE_CONTROL_CAPABILITIES = frozenset(
    {
        NativeConnectorCapability.ATTACH_EXISTING_SESSION,
        NativeConnectorCapability.STREAM_SESSION_STATE,
        NativeConnectorCapability.OBSERVE_APPROVAL_REQUESTS,
        NativeConnectorCapability.RESOLVE_APPROVAL_REQUESTS,
        NativeConnectorCapability.PRESERVE_PROVIDER_DECISIONS,
        NativeConnectorCapability.CONTINUE_SAME_EXECUTION_CONTEXT,
        NativeConnectorCapability.REVOKE_CONNECTION,
    }
)


class NativeConnectorManifest(BaseModel):
    """A provider adapter's explicit, evidence-backed product contract."""

    provider: str = Field(min_length=1, max_length=120)
    adapter_id: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=160)
    mode: NativeConnectorMode
    support: NativeConnectorSupport
    capabilities: list[NativeConnectorCapability] = Field(default_factory=list)
    public_contract: bool = False
    evidence_references: list[str] = Field(default_factory=list, max_length=20)
    notes: str = Field(default="", max_length=4000)

    @field_validator("provider", "adapter_id", "display_name")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value must not be blank")
        return cleaned

    @field_validator("capabilities")
    @classmethod
    def deduplicate_capabilities(
        cls,
        values: list[NativeConnectorCapability],
    ) -> list[NativeConnectorCapability]:
        return list(dict.fromkeys(values))


class NativeConnectorEligibility(BaseModel):
    native_control_eligible: bool
    mode: NativeConnectorMode
    support: NativeConnectorSupport
    missing_capabilities: list[NativeConnectorCapability] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    customer_label: str


def evaluate_native_control(
    manifest: NativeConnectorManifest,
) -> NativeConnectorEligibility:
    """Fail closed unless a public contract proves every native capability."""

    blockers: list[str] = []
    available = set(manifest.capabilities)
    missing = sorted(
        REQUIRED_NATIVE_CONTROL_CAPABILITIES - available,
        key=lambda item: item.value,
    )

    if manifest.mode != NativeConnectorMode.NATIVE_EXISTING_SESSION:
        blockers.append("adapter owns a managed runtime instead of an existing native session")
    if manifest.support != NativeConnectorSupport.PUBLIC_SUPPORTED:
        blockers.append(f"support level is {manifest.support.value}")
    if not manifest.public_contract:
        blockers.append("no documented public third-party connector contract is recorded")
    if missing:
        blockers.append("required native-session capabilities are missing")
    if not manifest.evidence_references:
        blockers.append("no provider evidence reference is recorded")

    eligible = not blockers
    return NativeConnectorEligibility(
        native_control_eligible=eligible,
        mode=manifest.mode,
        support=manifest.support,
        missing_capabilities=missing,
        blockers=blockers,
        customer_label=(
            "Native session"
            if manifest.mode == NativeConnectorMode.NATIVE_EXISTING_SESSION
            else "Aixion-managed session"
        ),
    )


def codex_public_connector_inventory() -> list[NativeConnectorManifest]:
    """Current conservative Codex inventory based on reviewed public material."""

    return [
        NativeConnectorManifest(
            provider="CODEX",
            adapter_id="codex-native-desktop",
            display_name="Codex native desktop session",
            mode=NativeConnectorMode.NATIVE_EXISTING_SESSION,
            support=NativeConnectorSupport.PARTNER_REQUIRED,
            capabilities=[],
            public_contract=False,
            evidence_references=[
                "https://openai.com/index/work-with-codex-from-anywhere/",
                "https://help.openai.com/en/articles/20001275-chatgpt-work-and-codex",
            ],
            notes=(
                "OpenAI documents first-party remote continuity through ChatGPT mobile, "
                "but the reviewed public material does not document third-party approval "
                "subscription and resolution for an already-running native Codex session."
            ),
        ),
        NativeConnectorManifest(
            provider="CODEX",
            adapter_id="codex-aixion-managed-app-server",
            display_name="Codex Aixion-managed app-server session",
            mode=NativeConnectorMode.AIXION_MANAGED_SESSION,
            support=NativeConnectorSupport.MANAGED_ONLY,
            capabilities=[
                NativeConnectorCapability.START_MANAGED_RUNTIME,
                NativeConnectorCapability.STRUCTURED_PROVIDER_EVENTS,
                NativeConnectorCapability.OBSERVE_APPROVAL_REQUESTS,
                NativeConnectorCapability.RESOLVE_APPROVAL_REQUESTS,
                NativeConnectorCapability.PRESERVE_PROVIDER_DECISIONS,
            ],
            public_contract=True,
            evidence_references=[
                "docs/research/local_codex_session_supervision_v1_results.md",
            ],
            notes=(
                "This adapter is an explicit fallback. It starts and owns a provider "
                "runtime and must never be presented as attachment to Codex Desktop."
            ),
        ),
    ]
