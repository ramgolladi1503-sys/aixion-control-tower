package com.aixion.controltower.feature.agentaccess

import android.app.Activity
import android.content.ClipData
import android.content.ClipDescription
import android.content.ContextWrapper
import android.os.Build
import android.os.PersistableBundle
import java.net.URI
import java.util.Locale
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
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
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.BuildConfig
import com.aixion.controltower.core.api.dto.AgentCredentialStatusDto
import com.aixion.controltower.data.repository.AgentAccessSummary
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerHeroPanel
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.components.TowerSectionHeader
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerCodeTextStyle
import com.aixion.controltower.core.ui.theme.towerScreenHorizontalPadding

@Composable
fun AgentAccessScreen(viewModel: AgentAccessViewModel = viewModel()) {
    val palette = LocalTowerPalette.current
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
        modifier = Modifier
            .fillMaxSize()
            .background(palette.canvas)
            .padding(horizontal = towerScreenHorizontalPadding(), vertical = TowerSpacing.lg),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
    ) {
        item {
            AgentAccessHero(provider = state.selectedProvider, isLoading = state.loading)
        }

        item { AgentAccessSetupGuide(state) }

        if (state.notice.isNotBlank()) item { Text(state.notice, color = palette.positive) }
        if (state.error.isNotBlank()) item { Text(state.error, color = palette.danger) }

        if (!state.loading && state.projectsLoaded && state.projects.isEmpty()) {
            item {
                TowerPanel(elevated = true) {
                    Text("Create your first project", color = palette.ink, style = MaterialTheme.typography.titleMedium)
                    Spacer(Modifier.height(TowerSpacing.xs))
                    Text(
                        "Projects keep each agent connector scoped to work in your account. New projects use Strict mode.",
                        color = palette.inkMuted,
                        style = MaterialTheme.typography.bodyMedium
                    )
                    Spacer(Modifier.height(TowerSpacing.sm))
                    OutlinedTextField(
                        value = state.newProjectName,
                        onValueChange = viewModel::setNewProjectName,
                        enabled = !state.working,
                        label = { Text("Project name") },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(Modifier.height(TowerSpacing.sm))
                    Button(
                        onClick = viewModel::createProject,
                        enabled = !state.working && !state.loading && state.projectsLoaded &&
                            !state.projectCreationNeedsRefresh && state.newProjectName.isNotBlank(),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(if (state.working) "Creating project…" else "Create project")
                    }
                }
            }
        }

        item {
            TowerPanel {
                Text("Choose platform", color = palette.ink, fontWeight = FontWeight.SemiBold)
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
                    onCopyKeychainCommand = { command ->
                        copyNonSecretCommand(context, "macOS Keychain command", command)
                    },
                    onCopyHookInstallCommand = { command ->
                        copyNonSecretCommand(context, "Agent hook install command", command)
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
                        enabled = !state.loading && !state.working && state.projects.isNotEmpty(),
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
                    enabled = canCreateApprovalAgent(state),
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
                    Text("No ${agentProviderName(state.selectedProvider)} connector enrolled", color = palette.ink)
                    Spacer(Modifier.height(TowerSpacing.sm))
                    Text("Create an agent above to generate a one-time token.", color = palette.inkMuted)
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
internal fun AgentAccessHero(provider: String, isLoading: Boolean) {
    val palette = LocalTowerPalette.current
    TowerHeroPanel {
        AgentAccessStatusBadge(isLoading = isLoading)
        Spacer(Modifier.height(TowerSpacing.md))
        Text(
            text = "Connect ${agentProviderName(provider)}",
            modifier = Modifier.semantics { heading() },
            color = palette.ink,
            style = MaterialTheme.typography.headlineMedium
        )
        Spacer(Modifier.height(TowerSpacing.sm))
        Text(
            "Create a project-limited connector credential here, then install its local hook in that project on your Mac. Approval decisions stay in this phone app.",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
    }
}

@Composable
internal fun AgentAccessSetupGuide(state: AgentAccessUiState) {
    val palette = LocalTowerPalette.current
    val selectedProject = state.projects.firstOrNull { it.id == state.selectedProjectId }
    val projectReady = state.projectsLoaded && selectedProject != null
    val connectorReady = projectReady && state.agents.any { summary ->
        summary.agent.provider == state.selectedProvider &&
            summary.agent.enabled &&
            summary.agent.allowed_project_ids == listOf(state.selectedProjectId) &&
            summary.credentialState == "ACTIVE" &&
            summary.credential?.token_present == true
    }
    val completePhoneSteps = listOf(projectReady, connectorReady).count { it }

    TowerPanel(elevated = true) {
        Column(
            modifier = Modifier.fillMaxWidth(),
            verticalArrangement = Arrangement.spacedBy(TowerSpacing.xs)
        ) {
            Text("Setup progress", color = palette.ink, style = MaterialTheme.typography.titleMedium)
            StatusBadge(
                label = if (state.loading && !state.projectsLoaded) "Loading" else "$completePhoneSteps of 2 phone steps",
                color = if (completePhoneSteps == 2) palette.positive else palette.caution,
                uppercase = false
            )
        }
        Spacer(Modifier.height(TowerSpacing.sm))
        SetupGuideStep(
            number = "1",
            title = "Choose a project",
            detail = when {
                selectedProject != null -> "Selected: ${selectedProject.name}"
                !state.projectsLoaded && state.loading -> "Loading projects from your account…"
                state.projectsLoaded -> "Select an existing project or create one below."
                else -> "Refresh projects before continuing."
            },
            complete = projectReady
        )
        Spacer(Modifier.height(TowerSpacing.sm))
        SetupGuideStep(
            number = "2",
            title = "Create a project connector",
            detail = if (connectorReady) {
                "An active project-scoped credential is available."
            } else {
                "Create it below. The token is shown once for secure installation on your Mac."
            },
            complete = connectorReady
        )
        Spacer(Modifier.height(TowerSpacing.md))
        StatusBadge(label = "Mac setup not confirmed", color = palette.caution, uppercase = false)
        Spacer(Modifier.height(TowerSpacing.xs))
        Text(
            "After creating the connector, run its local hook setup command on the computer. This app cannot confirm hook installation or same-session resume.",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        if (state.selectedProvider == "CODEX") {
            Spacer(Modifier.height(TowerSpacing.xs))
            Text(
                "Native Codex Desktop approval interception is not verified. Do not rely on it until that workflow is confirmed.",
                color = palette.caution,
                style = MaterialTheme.typography.bodyMedium
            )
        }
    }
}

@Composable
private fun SetupGuideStep(number: String, title: String, detail: String, complete: Boolean) {
    val palette = LocalTowerPalette.current
    Column(verticalArrangement = Arrangement.spacedBy(TowerSpacing.xs)) {
        Text(
            text = "$number. $title${if (complete) " · Done" else ""}",
            color = palette.ink,
            style = MaterialTheme.typography.titleSmall
        )
        Text(detail, color = palette.inkMuted, style = MaterialTheme.typography.bodyMedium)
    }
}

@Composable
internal fun AgentAccessStatusBadge(isLoading: Boolean) {
    val palette = LocalTowerPalette.current
    StatusBadge(
        label = if (isLoading) "Syncing" else "Host status unverified",
        color = palette.caution,
        uppercase = false
    )
}

@Composable
internal fun ConnectorPermissionsDisclosure() {
    val palette = LocalTowerPalette.current
    Column(verticalArrangement = Arrangement.spacedBy(TowerSpacing.sm)) {
        Text("Granted actions", color = palette.ink, style = MaterialTheme.typography.titleMedium)
        Text(
            "This connector can only:",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        Column(verticalArrangement = Arrangement.spacedBy(TowerSpacing.xs)) {
            listOf(
                "Create approval requests for this project",
                "Read a request’s recorded decision",
                "Fetch an approved decision once",
                "Report when decision delivery fails",
                "Expire its own approval requests"
            ).forEach { capability ->
                Text(
                    text = "• $capability",
                    color = palette.inkMuted,
                    style = MaterialTheme.typography.bodyMedium
                )
            }
        }
        Text(
            "Consume only returns an already-approved decision once. This credential cannot approve requests or start tools; the paused host agent continues only after that decision is returned.",
            color = palette.inkMuted,
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

private fun copyNonSecretCommand(context: android.content.Context, label: String, command: String): Boolean =
    runCatching {
        val clipboard = context.getSystemService(android.content.ClipboardManager::class.java)
            ?: return false
        clipboard.setPrimaryClip(ClipData.newPlainText(label, command))
        true
    }.getOrDefault(false)

internal fun keychainSetupCommand(agentId: String): String =
    "security add-generic-password -U -s com.aixion.controltower.agent -a ${shellQuote(agentId)} -w"

internal fun normalizeWorkspacePath(rawPath: String): String? {
    if (rawPath.isBlank() || rawPath != rawPath.trim() ||
        rawPath.any { it == '\u0000' || it == '\n' || it == '\r' }
    ) return null
    return runCatching {
        val path = java.nio.file.Paths.get(rawPath)
        if (!path.isAbsolute) return null
        val normalized = path.normalize()
        if (normalized == normalized.root) return null
        normalized.toString()
    }.getOrNull()
}

internal fun hookInstallCommand(
    provider: String,
    workspacePath: String,
    agentId: String,
    projectId: String,
    apiBaseUrl: String = BuildConfig.AIXION_API_BASE_URL
): String? {
    val normalizedWorkspace = normalizeWorkspacePath(workspacePath) ?: return null
    val normalizedApiBaseUrl = normalizedMacApiBaseUrl(apiBaseUrl) ?: return null
    val hookDirectory = if (provider == "CODEX") "codex" else "antigravity"
    return "python3 ${shellQuote("integrations/$hookDirectory/setup_workspace.py")} \\\n" +
        "  --workspace ${shellQuote(normalizedWorkspace)} \\\n" +
        "  --api-base-url ${shellQuote(normalizedApiBaseUrl)} \\\n" +
        "  --agent-id ${shellQuote(agentId)} \\\n" +
        "  --project-id ${shellQuote(projectId)}"
}

internal fun isMacCompatibleApiBaseUrl(rawUrl: String): Boolean =
    normalizedMacApiBaseUrl(rawUrl) != null

private fun normalizedMacApiBaseUrl(rawUrl: String): String? = runCatching {
    if (rawUrl != rawUrl.trim()) return null
    val value = rawUrl.trimEnd('/')
    if (value.isBlank() || '?' in value || '#' in value) return null
    val uri = URI(value)
    val host = uri.host?.removePrefix("[")?.removeSuffix("]")?.lowercase(Locale.ROOT)
    val port = uri.port
    if (port !in -1..65535) return null
    if (host.isNullOrBlank() || uri.rawUserInfo != null || uri.rawQuery != null || uri.rawFragment != null) {
        return null
    }
    val allowed = when {
        uri.scheme.equals("https", ignoreCase = true) -> true
        uri.scheme.equals("http", ignoreCase = true) -> host in setOf("localhost", "127.0.0.1", "::1")
        else -> false
    }
    value.takeIf { allowed }
}.getOrNull()

private fun shellQuote(value: String): String = "'${value.replace("'", "'\\''")}'"

internal fun authenticatedActivityLabel(credential: AgentCredentialStatusDto?): String = when {
    credential == null -> "Activity status unavailable"
    !credential.token_present -> "No active token; activity is unavailable"
    credential.last_used_at.isNullOrBlank() -> "No authenticated activity recorded"
    else -> "Last authenticated API activity: ${credential.last_used_at} (not a live connection check)"
}

@Composable
@OptIn(ExperimentalLayoutApi::class)
private fun AgentCard(agentSummary: AgentAccessSummary, working: Boolean, onRotate: () -> Unit, onRevoke: () -> Unit) {
    val palette = LocalTowerPalette.current
    val agent = agentSummary.agent
    TowerPanel(elevated = true) {
        Row(horizontalArrangement = Arrangement.SpaceBetween, modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.weight(1f)) {
                Text(agent.display_name, color = palette.ink, fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.height(4.dp))
                Text(agent.id, color = palette.inkMuted, style = MaterialTheme.typography.bodyMedium)
            }
            val credentialState = if (agent.enabled) agentSummary.credentialState else "DISABLED"
            val statusColor = when (credentialState) {
                "ACTIVE" -> palette.positive
                "UNKNOWN", "EXPIRED" -> palette.caution
                else -> palette.danger
            }
            StatusBadge(
                label = "Token ${credentialState.lowercase().replace('_', ' ')}",
                color = statusColor,
                uppercase = false
            )
        }
        Spacer(Modifier.height(TowerSpacing.sm))
        Text(
            authenticatedActivityLabel(agentSummary.credential),
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        Text(
            "Projects: ${agent.allowed_project_ids.joinToString().ifBlank { "none" }}",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        Text(
            "Actions: ${agent.allowed_actions.joinToString()}",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        FlowRow(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(TowerSpacing.sm),
            verticalArrangement = Arrangement.spacedBy(TowerSpacing.xs)
        ) {
            OutlinedButton(onClick = onRotate, enabled = !working && agent.enabled) { Text("Rotate token") }
            TextButton(
                onClick = onRevoke,
                enabled = !working && agent.enabled && agentSummary.credential?.token_present == true
            ) { Text("Revoke") }
        }
    }
}

@Composable
@OptIn(ExperimentalLayoutApi::class)
internal fun OneTimeTokenPanel(
    token: String,
    provider: String,
    agentName: String,
    agentId: String,
    projectId: String,
    apiBaseUrl: String = BuildConfig.AIXION_API_BASE_URL,
    onCopy: () -> Unit,
    onCopyKeychainCommand: (String) -> Boolean,
    onCopyHookInstallCommand: (String) -> Boolean,
    onHide: () -> Unit
) {
    val palette = LocalTowerPalette.current
    val providerName = agentProviderName(provider)
    var keychainCopyMessage by remember(provider, agentId, projectId) { mutableStateOf<String?>(null) }
    var hookCopyMessage by remember(provider, agentId, projectId) { mutableStateOf<String?>(null) }
    var workspacePath by remember(provider, agentId, projectId) { mutableStateOf("") }
    val normalizedWorkspacePath = normalizeWorkspacePath(workspacePath)
    val keychainCommand = keychainSetupCommand(agentId)
    val apiUrlUsable = isMacCompatibleApiBaseUrl(apiBaseUrl)
    val installCommand = if (apiUrlUsable) normalizedWorkspacePath?.let {
        hookInstallCommand(provider, it, agentId, projectId, apiBaseUrl)
    } else {
        null
    }
    TowerPanel(elevated = true) {
        StatusBadge(label = "One-time secret", color = palette.caution, uppercase = false)
        Spacer(Modifier.height(TowerSpacing.sm))
        Text(
            "Save this $providerName token in macOS Keychain now",
            modifier = Modifier
                .testTag("agent-token-created-announcement")
                .semantics { liveRegion = LiveRegionMode.Polite },
            color = palette.ink,
            fontWeight = FontWeight.SemiBold
        )
        Text(
            "It is held only in screen memory and is cleared when you hide it or leave the app.",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        Spacer(Modifier.height(TowerSpacing.sm))
        Text("Agent: $agentName", color = palette.inkMuted, style = MaterialTheme.typography.bodyMedium)
        Text("Agent ID: $agentId", color = palette.inkMuted, style = MaterialTheme.typography.bodyMedium)
        Text("Project ID: $projectId", color = palette.inkMuted, style = MaterialTheme.typography.bodyMedium)
        Spacer(Modifier.height(TowerSpacing.sm))
        Text(token, color = palette.ink, style = TowerCodeTextStyle)
        FlowRow(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(TowerSpacing.sm),
            verticalArrangement = Arrangement.spacedBy(TowerSpacing.xs)
        ) {
            Button(onClick = onCopy) { Text("Copy sensitive token") }
            TextButton(onClick = onHide) { Text("Hide now") }
        }
        Text(
            "Store the token in macOS Keychain. This command contains no token; Terminal prompts you to enter it securely.",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        Text(keychainCommand, color = palette.ink, style = TowerCodeTextStyle)
        OutlinedButton(
            onClick = {
                keychainCopyMessage = if (onCopyKeychainCommand(keychainCommand)) {
                    "Keychain command copied. Enter the token only at the secure prompt."
                } else {
                    "Could not copy the Keychain command. Try again."
                }
            },
            modifier = Modifier.fillMaxWidth()
        ) { Text("Copy Keychain command") }
        keychainCopyMessage?.let { message ->
            Text(
                message,
                color = if (message.startsWith("Could not")) palette.danger else palette.positive,
                style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite }
            )
        }
        Spacer(Modifier.height(TowerSpacing.sm))
        Text(
            "Install the ${providerName} hook only in the project you want to protect. Enter that project's absolute folder path on your Mac. Run the command from the Control Tower repository root, with this local hook repository available, and confirm the API URL in the command is reachable from your Mac. This does not install a Control Tower Mac app; it adds project-scoped hook settings and preserves other hooks.",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        OutlinedTextField(
            value = workspacePath,
            onValueChange = { workspacePath = it },
            label = { Text("Absolute local project directory") },
            placeholder = { Text("/Users/you/Projects/example") },
            supportingText = {
                Text(
                    if (workspacePath.isBlank() || normalizedWorkspacePath != null) {
                        "Use a normalized absolute Mac path, for example /Users/you/Projects/example."
                    } else {
                        "Enter a valid absolute project path, such as /Users/you/Projects/example."
                    }
                )
            },
            isError = workspacePath.isNotBlank() && normalizedWorkspacePath == null,
            modifier = Modifier.fillMaxWidth()
        )
        if (!apiUrlUsable) {
            Text(
                "This app build's API URL cannot be used from the Mac. Configure an HTTPS endpoint, or localhost/127.0.0.1/::1 for development, then rebuild the app. The hook command is unavailable until then.",
                color = palette.danger,
                style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite }
            )
        }
        installCommand?.let { command ->
            Text(command, color = palette.ink, style = TowerCodeTextStyle)
        }
        OutlinedButton(
            onClick = {
                val command = installCommand ?: return@OutlinedButton
                hookCopyMessage = if (onCopyHookInstallCommand(command)) {
                    "Hook install command copied. Run it from the Control Tower repository root."
                } else {
                    "Could not copy the hook install command. Try again."
                }
            },
            enabled = installCommand != null,
            modifier = Modifier.fillMaxWidth()
        ) { Text("Copy hook install command") }
        hookCopyMessage?.let { message ->
            Text(
                message,
                color = if (message.startsWith("Could not")) palette.danger else palette.positive,
                style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite }
            )
        }
        if (provider == "CODEX") {
            Spacer(Modifier.height(TowerSpacing.sm))
            Text(
                "Open that project in Codex and review/trust the PermissionRequest hook when prompted. It covers local permission prompts only; verify the same paused Codex action continues after approval.",
                color = palette.inkMuted,
                style = MaterialTheme.typography.bodyMedium
            )
        }
    }
}

private fun agentProviderName(provider: String): String = when (provider) {
    "CODEX" -> "Codex"
    else -> "Antigravity"
}
