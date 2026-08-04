package com.aixion.controltower.core.api.dto

import com.google.gson.annotations.SerializedName

data class RelayAdapterManifestDto(
    @SerializedName("adapter_id") val adapterId: String,
    val provider: String,
    @SerializedName("adapter_kind") val adapterKind: String,
    @SerializedName("display_name") val displayName: String,
    val version: String = "unknown",
    val executable: String? = null,
    val available: Boolean = true,
    val features: List<String> = emptyList(),
    @SerializedName("external_agent_id") val externalAgentId: String? = null,
    val metadata: Map<String, Any?> = emptyMap()
)

data class RelayHostDto(
    val id: String,
    val name: String,
    val platform: String,
    val hostname: String,
    @SerializedName("machine_fingerprint") val machineFingerprint: String,
    val status: String,
    @SerializedName("workspace_roots") val workspaceRoots: List<String> = emptyList(),
    @SerializedName("allowed_project_ids") val allowedProjectIds: List<String> = emptyList(),
    @SerializedName("allowed_repositories") val allowedRepositories: List<String> = emptyList(),
    val adapters: List<RelayAdapterManifestDto> = emptyList(),
    @SerializedName("active_session_count") val activeSessionCount: Int = 0,
    @SerializedName("relay_version") val relayVersion: String = "unknown",
    @SerializedName("last_heartbeat_at") val lastHeartbeatAt: String? = null,
    @SerializedName("disabled_at") val disabledAt: String? = null,
    @SerializedName("disabled_reason") val disabledReason: String = "",
    @SerializedName("created_at") val createdAt: String? = null,
    @SerializedName("updated_at") val updatedAt: String? = null
)

data class RelaySummaryDto(
    @SerializedName("total_relays") val totalRelays: Int = 0,
    @SerializedName("online_relays") val onlineRelays: Int = 0,
    @SerializedName("offline_relays") val offlineRelays: Int = 0,
    @SerializedName("degraded_relays") val degradedRelays: Int = 0,
    @SerializedName("total_sessions") val totalSessions: Int = 0,
    @SerializedName("active_sessions") val activeSessions: Int = 0,
    @SerializedName("waiting_for_approval") val waitingForApproval: Int = 0,
    @SerializedName("failed_sessions") val failedSessions: Int = 0,
    @SerializedName("pending_commands") val pendingCommands: Int = 0,
    @SerializedName("leased_commands") val leasedCommands: Int = 0
)

data class RelaySessionDto(
    val id: String,
    @SerializedName("relay_id") val relayId: String,
    val provider: String,
    @SerializedName("adapter_id") val adapterId: String,
    val objective: String,
    @SerializedName("workspace_path") val workspacePath: String,
    val repository: String? = null,
    @SerializedName("project_id") val projectId: String? = null,
    @SerializedName("task_id") val taskId: String? = null,
    @SerializedName("run_id") val runId: String? = null,
    @SerializedName("approval_mode") val approvalMode: String = "STRICT",
    val model: String? = null,
    val status: String,
    @SerializedName("remote_session_id") val remoteSessionId: String? = null,
    @SerializedName("current_turn_id") val currentTurnId: String? = null,
    @SerializedName("latest_event_sequence") val latestEventSequence: Int = 0,
    @SerializedName("final_evidence_hash") val finalEvidenceHash: String? = null,
    @SerializedName("last_error") val lastError: String? = null,
    @SerializedName("max_runtime_seconds") val maxRuntimeSeconds: Int = 3600,
    @SerializedName("started_at") val startedAt: String? = null,
    @SerializedName("completed_at") val completedAt: String? = null,
    @SerializedName("created_at") val createdAt: String? = null,
    @SerializedName("updated_at") val updatedAt: String? = null
)

data class RelayCommandDto(
    val id: String,
    @SerializedName("relay_id") val relayId: String,
    @SerializedName("session_id") val sessionId: String? = null,
    @SerializedName("command_type") val commandType: String,
    val status: String,
    @SerializedName("attempt_count") val attemptCount: Int = 0,
    @SerializedName("max_attempts") val maxAttempts: Int = 3,
    @SerializedName("lease_owner") val leaseOwner: String? = null,
    @SerializedName("lease_expires_at") val leaseExpiresAt: String? = null,
    @SerializedName("acknowledged_at") val acknowledgedAt: String? = null,
    @SerializedName("completed_at") val completedAt: String? = null,
    val error: String? = null,
    @SerializedName("created_at") val createdAt: String? = null,
    @SerializedName("updated_at") val updatedAt: String? = null
)

data class RelayEventDto(
    val id: String,
    @SerializedName("relay_id") val relayId: String,
    @SerializedName("session_id") val sessionId: String,
    val provider: String,
    @SerializedName("adapter_id") val adapterId: String,
    @SerializedName("event_id") val eventId: String,
    val sequence: Int,
    @SerializedName("event_type") val eventType: String,
    val message: String = "",
    @SerializedName("remote_session_id") val remoteSessionId: String? = null,
    @SerializedName("remote_turn_id") val remoteTurnId: String? = null,
    @SerializedName("event_hash") val eventHash: String,
    @SerializedName("created_at") val createdAt: String? = null,
    @SerializedName("received_at") val receivedAt: String? = null
)

data class RelaySessionDetailDto(
    val session: RelaySessionDto,
    val relay: RelayHostDto,
    val commands: List<RelayCommandDto> = emptyList(),
    val events: List<RelayEventDto> = emptyList()
)

data class RelaySessionCreateDto(
    @SerializedName("relay_id") val relayId: String,
    val provider: String,
    @SerializedName("adapter_id") val adapterId: String,
    val objective: String,
    @SerializedName("workspace_path") val workspacePath: String,
    val repository: String? = null,
    @SerializedName("project_id") val projectId: String? = null,
    @SerializedName("task_id") val taskId: String? = null,
    @SerializedName("run_id") val runId: String? = null,
    @SerializedName("approval_mode") val approvalMode: String = "STRICT",
    val model: String? = null,
    @SerializedName("max_runtime_seconds") val maxRuntimeSeconds: Int = 3600
)

data class RelaySessionMessageDto(
    val message: String,
    val metadata: Map<String, Any?> = emptyMap()
)

data class RelaySessionControlDto(val reason: String = "")
