package com.aixion.controltower.feature.agentaccess

import android.app.Activity
import android.content.ClipData
import android.content.ClipDescription
import android.content.ContextWrapper
import android.os.Build
import android.os.PersistableBundle
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.api.dto.AgentCredentialStatusDto
import com.aixion.controltower.data.repository.AgentAccessSummary
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerHeroPanel
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.components.TowerSectionHeader
import com.aixion.controltower.core.ui.theme.RiskBlocked
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerCodeTextStyle
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
fun AgentAccessScreen(viewModel: AgentAccessViewModel = viewModel()) {
    val state by viewModel.state.collectAsState()
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val tokenCurrentlyVisible = state.oneTimeToken
    val latestVisibleToken by rememberUpdatedState(tokenCurrentlyVisible)
    var projectMenuOpen by remember { mutableStateOf(false) }
    var providerMenuOpen by remember { mutableStateOf(false) }
    var pendingAction by remember { mutableStateOf<Pair<String, String>?>(null) }

    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_STOP) {
                clearClipboardToken(context, latestVisibleToken)
                viewModel.clearOneTimeToken()
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
            clearClipboardToken(context, latestVisibleToken)
            if (latestVisibleToken != null) viewModel.clearOneTimeToken()
        }
    }

    val activityWindow = remember(context) {
        generateSequence(context) { (it as? ContextWrapper)?.baseContext }
            .filterIsInstance<Activity>()
            .firstOrNull()
            ?.window
    }
    DisposableEffect(activityWindow, tokenCurrentlyVisible) {
        val wasAlreadySecure = activityWindow?.attributes?.flags
            ?.and(android.view.WindowManager.LayoutParams.FLAG_SECURE) != 0
        if (tokenCurrentlyVisible != null) {
            activityWindow?.addFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE)
        }
        onDispose {
            if (tokenCurrentlyVisible != null && !wasAlreadySecure) {
                activityWindow?.clearFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE)
            }
        }
    }

    pendingAction?.let { (action, agentId) ->
        AlertDialog(
            onDismissRequest = { pendingAction = null },
            title = { Text(if (action == "rotate") "Rotate agent token?" else "Revoke agent token?") },
            text = {
                Text(
                    if (action == "rotate") {
                        "Rotation immediately invalidates the token currently installed on the Mac. The replacement is shown once."
                    } else {
                        "Revocation immediately blocks this agent from creating approvals or reading decisions."
                    }
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    pendingAction = null
                    if (action == "rotate") viewModel.rotateToken(agentId) else viewModel.revokeToken(agentId)
                }) { Text(if (action == "rotate") "Rotate" else "Revoke") }
            },
            dismissButton = { TextButton(onClick = { pendingAction = null }) { Text("Cancel") } }
        )
    }

    LazyColumn(
        modifier = Modifier.fillMaxSize().background(TowerBackground).padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
    ) {
        item {
            TowerHeroPanel {
                StatusBadge(label = if (state.loading) "SYNCING" else "MAC AGENT ACCESS", color = if (state.loading) RiskMedium else RiskLow)
                Spacer(Modifier.height(TowerSpacing.md))
                Text("Connect ${agentProviderName(state.selectedProvider)}", color = TowerTextPrimary, fontSize = 26.sp)
                Spacer(Modifier.height(TowerSpacing.sm))
                Text(
                    "Create a project-limited connector credential here, then install its local hook in that project on your Mac. Approval decisions stay in this phone app.",
                    color = TowerTextMuted,
                    fontSize = 14.sp,
                    lineHeight = 20.sp
                )
            }
        }

        if (state.notice.isNotBlank()) item { Text(state.notice, color = RiskLow) }
        if (state.error.isNotBlank()) item { Text(state.error, color = RiskBlocked) }

        item {
            TowerPanel {
                Text("Choose platform", color = TowerTextPrimary, fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.height(TowerSpacing.sm))
                Box {
                    OutlinedButton(
                        onClick = { providerMenuOpen = true },
                        enabled = !state.working && !state.loading && pendingAction == null,
                        modifier = Modifier.fillMaxWidth()
                    ) { Text(agentProviderName(state.selectedProvider)) }
                    DropdownMenu(
                        expanded = providerMenuOpen,
                        onDismissRequest = { providerMenuOpen = false }
                    ) {
                        listOf("ANTIGRAVITY", "CODEX").forEach { provider ->
                            DropdownMenuItem(
                                text = { Text(agentProviderName(provider)) },
                                onClick = {
                                    providerMenuOpen = false
                                    viewModel.selectProvider(provider)
                                }
                            )
                        }
                    }
                }
            }
        }

        state.oneTimeToken?.let { token ->
            item {
                OneTimeTokenPanel(
                    token = token,
                    provider = state.oneTimeProvider ?: state.selectedProvider,
                    agentName = state.oneTimeAgentName.orEmpty(),
                    agentId = state.oneTimeAgentId.orEmpty(),
                    projectId = state.oneTimeProjectId.orEmpty(),
                    onCopy = {
                        val clip = ClipData.newPlainText("Platform agent token", token)
                        if (Build.VERSION.SDK_INT >= 33) {
                            clip.description.extras = PersistableBundle().apply {
                                putBoolean(ClipDescription.EXTRA_IS_SENSITIVE, true)
                            }
                        }
                        context.getSystemService(android.content.ClipboardManager::class.java).setPrimaryClip(clip)
                    },
                    onHide = {
                        clearClipboardToken(context, token)
                        viewModel.clearOneTimeToken()
                    }
                )
            }
        }

        item {
            TowerSectionHeader(
                title = "Create ${agentProviderName(state.selectedProvider)} connector",
                subtitle = "The credential is limited to one selected project and five approval-only actions."
            )
        }
        item {
            TowerPanel(elevated = true) {
                OutlinedTextField(
                    value = state.displayName,
                    onValueChange = viewModel::setDisplayName,
                    label = { Text("Connector name") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )
                Spacer(Modifier.height(TowerSpacing.sm))
                val selectedProject = state.projects.firstOrNull { it.id == state.selectedProjectId }
                Box {
                    OutlinedButton(
                        onClick = { projectMenuOpen = true },
                        enabled = state.projects.isNotEmpty(),
                        modifier = Modifier.fillMaxWidth()
                    ) { Text(selectedProject?.name ?: "Select a project") }
                    DropdownMenu(expanded = projectMenuOpen, onDismissRequest = { projectMenuOpen = false }) {
                        state.projects.forEach { project ->
                            DropdownMenuItem(
                                text = { Text(project.name) },
                                onClick = {
                                    viewModel.selectProject(project.id)
                                    projectMenuOpen = false
                                }
                            )
                        }
                    }
                }
                Spacer(Modifier.height(TowerSpacing.sm))
                ConnectorPermissionsDisclosure()
                Spacer(Modifier.height(TowerSpacing.md))
                Button(
                    onClick = viewModel::createAgent,
                    enabled = !state.working && !state.loading && state.selectedProjectId != null,
                    modifier = Modifier.fillMaxWidth()
                ) { Text(if (state.working) "Working…" else "Create connector and issue token") }
            }
        }

        item {
            TowerSectionHeader(
                title = "Existing ${agentProviderName(state.selectedProvider)} connectors",
                subtitle = "Rotate or revoke credentials when a Mac is replaced, lost, or no longer trusted."
            )
        }
        if (!state.loading && state.agents.isEmpty()) {
            item {
                TowerPanel {
                    Text("No ${agentProviderName(state.selectedProvider)} connector enrolled", color = TowerTextPrimary)
                    Spacer(Modifier.height(TowerSpacing.sm))
                    Text("Create an agent above to generate a one-time token.", color = TowerTextMuted)
                }
            }
        }
        items(state.agents, key = { it.agent.id }) { agent ->
            AgentCard(
                agentSummary = agent,
                working = state.working,
                onRotate = { pendingAction = "rotate" to agent.agent.id },
                onRevoke = { pendingAction = "revoke" to agent.agent.id }
            )
        }
        item {
            OutlinedButton(onClick = viewModel::refresh, enabled = !state.working, modifier = Modifier.fillMaxWidth()) {
                Text("Refresh agent status")
            }
        }
    }
}

@Composable
internal fun ConnectorPermissionsDisclosure() {
    Column(verticalArrangement = Arrangement.spacedBy(TowerSpacing.sm)) {
        Text("Granted actions", color = TowerTextPrimary, style = MaterialTheme.typography.titleMedium)
        Text(
            "This connector is limited to the selected project and these five approval-only actions:",
            color = TowerTextMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        Column(verticalArrangement = Arrangement.spacedBy(TowerSpacing.xs)) {
            listOf(
                "CREATE_APPROVAL",
                "READ_APPROVAL_DECISION",
                "CONSUME_APPROVAL_DECISION",
                "REPORT_APPROVAL_DELIVERY_FAILURE",
                "EXPIRE_OWN_APPROVAL"
            ).forEach { action ->
                Text("• $action", color = TowerTextMuted, style = TowerCodeTextStyle)
            }
        }
        Text(
            "Consume only returns an already-approved decision once. This credential cannot approve requests or start tools; the paused host agent continues only after that decision is returned.",
            color = TowerTextMuted,
            style = MaterialTheme.typography.bodyLarge
        )
    }
}

private fun clearClipboardToken(context: android.content.Context, token: String?) {
    if (token.isNullOrEmpty()) return
    val clipboard = context.getSystemService(android.content.ClipboardManager::class.java) ?: return
    val copied = runCatching {
        clipboard.primaryClip?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(context)?.toString()
    }.getOrNull()
    if (copied == token) {
        if (Build.VERSION.SDK_INT >= 28) clipboard.clearPrimaryClip()
        else clipboard.setPrimaryClip(ClipData.newPlainText("", ""))
    }
}

internal fun authenticatedActivityLabel(credential: AgentCredentialStatusDto?): String = when {
    credential == null -> "Activity status unavailable"
    !credential.token_present -> "No active token; activity is unavailable"
    credential.last_used_at.isNullOrBlank() -> "No authenticated activity recorded"
    else -> "Last authenticated API activity: ${credential.last_used_at} (not a live connection check)"
}

@Composable
private fun AgentCard(agentSummary: AgentAccessSummary, working: Boolean, onRotate: () -> Unit, onRevoke: () -> Unit) {
    val agent = agentSummary.agent
    TowerPanel(elevated = true) {
        Row(horizontalArrangement = Arrangement.SpaceBetween, modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.weight(1f)) {
                Text(agent.display_name, color = TowerTextPrimary, fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.height(4.dp))
                Text(agent.id, color = TowerTextMuted, fontSize = 11.sp)
            }
            val credentialState = if (agent.enabled) agentSummary.credentialState else "DISABLED"
            val statusColor = when (credentialState) {
                "ACTIVE" -> RiskLow
                "UNKNOWN", "EXPIRED" -> RiskMedium
                else -> RiskBlocked
            }
            StatusBadge(label = "TOKEN $credentialState", color = statusColor)
        }
        Spacer(Modifier.height(TowerSpacing.sm))
        Text(
            authenticatedActivityLabel(agentSummary.credential),
            color = TowerTextMuted,
            fontSize = 12.sp,
            lineHeight = 17.sp
        )
        Text("Projects: ${agent.allowed_project_ids.joinToString().ifBlank { "none" }}", color = TowerTextMuted, fontSize = 12.sp)
        Text("Actions: ${agent.allowed_actions.joinToString()}", color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = onRotate, enabled = !working && agent.enabled) { Text("Rotate token") }
            TextButton(
                onClick = onRevoke,
                enabled = !working && agent.enabled && agentSummary.credential?.token_present == true
            ) { Text("Revoke") }
        }
    }
}

@Composable
internal fun OneTimeTokenPanel(
    token: String,
    provider: String,
    agentName: String,
    agentId: String,
    projectId: String,
    onCopy: () -> Unit,
    onHide: () -> Unit
) {
    val providerName = agentProviderName(provider)
    val hookDirectory = if (provider == "CODEX") "codex" else "antigravity"
    TowerPanel(elevated = true) {
        StatusBadge(label = "ONE-TIME SECRET", color = RiskMedium)
        Spacer(Modifier.height(TowerSpacing.sm))
        Text("Save this $providerName token in macOS Keychain now", color = TowerTextPrimary, fontWeight = FontWeight.SemiBold)
        Text(
            "It is held only in screen memory and is cleared when you hide it or leave the app.",
            color = TowerTextMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        Spacer(Modifier.height(TowerSpacing.sm))
        Text("Agent: $agentName", color = TowerTextMuted, style = MaterialTheme.typography.bodyMedium)
        Text("Agent ID: $agentId", color = TowerTextMuted, style = MaterialTheme.typography.bodyMedium)
        Text("Project ID: $projectId", color = TowerTextMuted, style = MaterialTheme.typography.bodyMedium)
        Spacer(Modifier.height(TowerSpacing.sm))
        Text(token, color = TowerTextPrimary, style = TowerCodeTextStyle)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = onCopy) { Text("Copy sensitive token") }
            TextButton(onClick = onHide) { Text("Hide now") }
        }
        Text(
            "On the Mac, run: security add-generic-password -U -s com.aixion.controltower.agent -a $agentId -w\nThe command prompts for the token; do not put it in command arguments or shell history.",
            color = TowerTextMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        Spacer(Modifier.height(TowerSpacing.sm))
        Text(
            "Next, install the $providerName hook only in the project you want to protect. Use the Control Tower API URL reachable from your Mac and that project's absolute folder path. The installer writes project-scoped settings and preserves other hooks.",
            color = TowerTextMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        Text(
            "python3 /ABSOLUTE/PATH/integrations/$hookDirectory/setup_workspace.py \\\n  --workspace \"/ABSOLUTE/PATH/TO/PROJECT\" \\\n  --api-base-url \"https://CONTROL-TOWER-URL\" \\\n  --agent-id \"$agentId\" \\\n  --project-id \"$projectId\"",
            color = TowerTextPrimary,
            style = TowerCodeTextStyle
        )
        if (provider == "CODEX") {
            Spacer(Modifier.height(TowerSpacing.sm))
            Text(
                "Open that project in Codex and review/trust the PermissionRequest hook when prompted. It covers local permission prompts only; verify the same paused Codex action continues after approval.",
                color = TowerTextMuted,
                style = MaterialTheme.typography.bodyMedium
            )
        }
    }
}

private fun agentProviderName(provider: String): String = when (provider) {
    "CODEX" -> "Codex"
    else -> "Antigravity"
}
