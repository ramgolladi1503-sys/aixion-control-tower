package com.aixion.controltower.feature.connectors

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
import androidx.compose.material3.Button
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.api.dto.ConnectorDto
import com.aixion.controltower.core.api.dto.ConnectorTemplateDto
import com.aixion.controltower.core.api.dto.webhookNativeSessionLimitation
import com.aixion.controltower.core.ui.components.ForgedLogoMark
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerHeroPanel
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.components.TowerSectionHeader
import com.aixion.controltower.core.ui.theme.RiskBlocked
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

private data class ConnectorSetupGuidance(
    val stage: String,
    val nextAction: String,
    val healthy: Boolean
)

@Composable
fun ConnectorsScreen(viewModel: ConnectorsViewModel = viewModel()) {
    val state by viewModel.state.collectAsState()
    val clipboard = LocalClipboardManager.current

    ConnectorsContent(
        state = state,
        onCopy = { label, value ->
            clipboard.setText(AnnotatedString(value))
            viewModel.markCopied(label)
        },
        onHideOneTimeSecret = viewModel::hideOneTimeSecret,
        onTemplateSelected = viewModel::selectTemplate,
        onCreateSelectedTemplateConnector = viewModel::createSelectedTemplateConnector,
        onToggle = viewModel::toggle,
        onIssueSecret = viewModel::issueSecret,
        onRevokeSecret = viewModel::revokeSecret,
        onApplyMapper = viewModel::applyTemplateMapper,
        onPreviewMapper = viewModel::previewTemplateMapper
    )
}

@Composable
fun ConnectorsContent(
    state: ConnectorsUiState,
    onCopy: (String, String) -> Unit = { _, _ -> },
    onHideOneTimeSecret: () -> Unit = {},
    onTemplateSelected: (String) -> Unit = {},
    onCreateSelectedTemplateConnector: () -> Unit = {},
    onToggle: (ConnectorDto) -> Unit = {},
    onIssueSecret: (ConnectorDto) -> Unit = {},
    onRevokeSecret: (ConnectorDto) -> Unit = {},
    onApplyMapper: (ConnectorDto) -> Unit = {},
    onPreviewMapper: (ConnectorDto) -> Unit = {}
) {
    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
    ) {
        item {
            ConnectorsHero(state = state)
            if (state.notice.isNotBlank()) {
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(state.notice, color = RiskLow, fontSize = 13.sp, lineHeight = 18.sp)
            }
            if (state.error.isNotBlank()) {
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(state.error, color = RiskBlocked, fontSize = 13.sp, lineHeight = 18.sp)
            }
            if (state.lastSecret.isNotBlank()) {
                OneTimeCredentialPanel(
                    secret = state.lastSecret,
                    onCopy = { onCopy("One-time connector credential", state.lastSecret) },
                    onHide = onHideOneTimeSecret
                )
            }
            if (state.preview.isNotBlank()) {
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(state.preview, color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
            }
        }

        item { FirstTimeConnectorGuide() }

        item {
            TemplatePanel(
                templates = state.templates,
                selectedTemplateId = state.selectedTemplateId,
                onTemplateSelected = onTemplateSelected,
                onCreate = onCreateSelectedTemplateConnector
            )
        }

        item {
            TowerSectionHeader(
                title = "Your connections",
                subtitle = if (state.connectors.isEmpty()) {
                    "No platforms connected yet. Choose one above to get started."
                } else {
                    "Check each connection's status and follow its next setup step."
                }
            )
        }

        items(state.connectors) { connector ->
            ConnectorCard(
                connector = connector,
                selectedTemplate = state.selectedTemplate,
                webhookUrl = state.webhookUrl(connector),
                onCopyWebhook = { onCopy("Webhook URL", state.webhookUrl(connector)) },
                onCopySetup = { onCopy("Connector setup", state.setupText(connector, state.selectedTemplate)) },
                onToggle = { onToggle(connector) },
                onIssueSecret = { onIssueSecret(connector) },
                onRevokeSecret = { onRevokeSecret(connector) },
                onApplyMapper = { onApplyMapper(connector) },
                onPreviewMapper = { onPreviewMapper(connector) }
            )
        }
    }
}

@Composable
private fun ConnectorsHero(state: ConnectorsUiState) {
    TowerHeroPanel {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.Top
        ) {
            Column(modifier = Modifier.weight(1f)) {
                StatusBadge(label = if (state.loading) "SYNCING" else "PLATFORM SETUP", color = if (state.loading) RiskMedium else RiskLow)
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Text("Connect a platform", color = TowerTextPrimary, fontSize = 28.sp, fontWeight = FontWeight.SemiBold)
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    text = if (state.loading) "Loading connections..." else "Set up webhook task intake here. Antigravity has a separate local hook setup; its live phone round trip is still being verified. Native Codex permission approvals are not connected yet.",
                    color = TowerTextMuted,
                    fontSize = 14.sp,
                    lineHeight = 20.sp
                )
            }
            ForgedLogoMark(size = 52.dp, color = TowerTextPrimary.copy(alpha = 0.78f))
        }
        Spacer(modifier = Modifier.height(TowerSpacing.lg))
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = "CONNECTORS ${state.connectors.size}", color = TowerAccent)
            StatusBadge(label = "TEMPLATES ${state.templates.size}", color = RiskMedium)
        }
    }
}

@Composable
private fun FirstTimeConnectorGuide() {
    TowerPanel(elevated = true) {
        Text("How to connect", color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text("1. Choose a webhook integration to submit task requests.", color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
        Text("2. Create its connection and store the one-time credential securely.", color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
        Text("3. Configure the external webhook and send a test task.", color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
        Text("4. Review any resulting approval request in Approvals. Webhooks do not pause or resume a native agent session.", color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
    }
}

@Composable
private fun OneTimeCredentialPanel(
    secret: String,
    onCopy: () -> Unit,
    onHide: () -> Unit
) {
    TowerPanel(modifier = Modifier.padding(top = 10.dp), elevated = true) {
        Text("One-time connector credential", color = TowerTextPrimary, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text("Copy this now. Hide it after storing it in the external agent config.", color = RiskMedium, fontSize = 12.sp, lineHeight = 17.sp)
        Text(secret, color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
        Column(verticalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            Button(onClick = onCopy, modifier = Modifier.fillMaxWidth()) { Text("Copy credential") }
            OutlinedButton(onClick = onHide, modifier = Modifier.fillMaxWidth()) { Text("Hide") }
        }
    }
}

@Composable
private fun TemplatePanel(
    templates: List<ConnectorTemplateDto>,
    selectedTemplateId: String?,
    onTemplateSelected: (String) -> Unit,
    onCreate: () -> Unit
) {
    TowerPanel(elevated = true) {
        Text("Choose a webhook integration", color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text("These integrations submit task requests. They do not install native approval hooks or resume a paused agent session.", color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        val selectedTemplate = templates.firstOrNull { it.id == selectedTemplateId }
        var platformMenuExpanded by remember { mutableStateOf(false) }
        Box {
            OutlinedButton(
                onClick = { platformMenuExpanded = true },
                enabled = templates.isNotEmpty(),
                modifier = Modifier.fillMaxWidth()
            ) {
                Text(selectedTemplate?.display_name ?: "Select a platform", lineHeight = 18.sp)
            }
            DropdownMenu(
                expanded = platformMenuExpanded,
                onDismissRequest = { platformMenuExpanded = false }
            ) {
                templates.forEach { template ->
                    DropdownMenuItem(
                        text = { Text(template.display_name) },
                        onClick = {
                            platformMenuExpanded = false
                            onTemplateSelected(template.id)
                        }
                    )
                }
            }
        }
        selectedTemplate?.let { template ->
            Text(template.description, color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
            webhookNativeSessionLimitation(template.provider_label, template.connector_type)?.let { limitation ->
                Text(limitation, color = RiskMedium, fontSize = 12.sp, lineHeight = 17.sp)
            }
            if (template.setup_notes.isNotEmpty()) {
                Text("Setup instructions", color = TowerTextPrimary, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                template.setup_notes.forEach { note ->
                    Text("• $note", color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
                }
            }
        }
        Button(onClick = onCreate, enabled = selectedTemplate != null, modifier = Modifier.fillMaxWidth()) {
            Text("Create connection")
        }
    }
}

@Composable
private fun ConnectorCard(
    connector: ConnectorDto,
    selectedTemplate: ConnectorTemplateDto?,
    webhookUrl: String,
    onCopyWebhook: () -> Unit,
    onCopySetup: () -> Unit,
    onToggle: () -> Unit,
    onIssueSecret: () -> Unit,
    onRevokeSecret: () -> Unit,
    onApplyMapper: () -> Unit,
    onPreviewMapper: () -> Unit
) {
    val guidance = connector.setupGuidance()
    var advancedSettingsExpanded by remember(connector.id) { mutableStateOf(false) }

    TowerPanel(elevated = true) {
        Column(verticalArrangement = Arrangement.spacedBy(TowerSpacing.sm)) {
            Text(connector.name, color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold, lineHeight = 23.sp)
            Text(connector.provider_label, color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
            StatusBadge(connector.status, if (connector.status == "ENABLED") RiskLow else RiskBlocked)
            StatusBadge(connector.health_status, if (connector.health_status == "HEALTHY") RiskLow else RiskMedium)
        }

        Spacer(modifier = Modifier.height(TowerSpacing.md))
        TowerPanel(elevated = false) {
            StatusBadge("STAGE ${guidance.stage.uppercase()}", if (guidance.healthy) RiskLow else RiskMedium)
            Spacer(modifier = Modifier.height(TowerSpacing.sm))
            Text("Next action", color = TowerTextPrimary, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
            Text(guidance.nextAction, color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
        }

        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Text("Webhook", color = TowerTextPrimary, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
        Text(webhookUrl, color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
        Column(verticalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            OutlinedButton(onClick = onCopyWebhook, modifier = Modifier.fillMaxWidth()) { Text("Copy webhook") }
            OutlinedButton(onClick = onCopySetup, modifier = Modifier.fillMaxWidth()) { Text("Copy setup block") }
        }

        if (!connector.secret_configured) {
            Button(onClick = onIssueSecret, modifier = Modifier.fillMaxWidth()) {
                Text("Create connection credential")
            }
        }

        OutlinedButton(
            onClick = { advancedSettingsExpanded = !advancedSettingsExpanded },
            modifier = Modifier.fillMaxWidth()
        ) {
            Text(if (advancedSettingsExpanded) "Hide advanced settings" else "Advanced settings")
        }

        if (advancedSettingsExpanded) {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
                Text("Actions: ${connector.allowed_actions.joinToString()}", color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
                Text("Repositories: ${connector.allowed_repositories.joinToString().ifBlank { "wildcard" }}", color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
                Text("Last used: ${connector.last_used_at ?: "never"}", color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
                if (connector.failed_auth_count > 0 || !connector.last_error.isNullOrBlank()) {
                    Text("Authentication failures: ${connector.failed_auth_count}", color = RiskBlocked, fontSize = 12.sp, lineHeight = 17.sp)
                    connector.last_error?.takeIf(String::isNotBlank)?.let {
                        Text("Last error: $it", color = RiskBlocked, fontSize = 12.sp, lineHeight = 17.sp)
                    }
                }
                OutlinedButton(onClick = onToggle, modifier = Modifier.fillMaxWidth()) {
                    Text(if (connector.status == "ENABLED") "Disable connection" else "Enable connection")
                }
                if (connector.secret_configured) {
                    OutlinedButton(onClick = onIssueSecret, modifier = Modifier.fillMaxWidth()) {
                        Text("Rotate credential")
                    }
                    OutlinedButton(onClick = onRevokeSecret, modifier = Modifier.fillMaxWidth()) {
                        Text("Revoke credential")
                    }
                }
                OutlinedButton(onClick = onApplyMapper, enabled = selectedTemplate != null, modifier = Modifier.fillMaxWidth()) {
                    Text("Apply selected mapper")
                }
                OutlinedButton(onClick = onPreviewMapper, enabled = selectedTemplate != null, modifier = Modifier.fillMaxWidth()) {
                    Text("Test selected template payload")
                }
            }
        }
    }
}

private fun ConnectorDto.setupGuidance(): ConnectorSetupGuidance {
    return when {
        failed_auth_count > 0 || !last_error.isNullOrBlank() -> ConnectorSetupGuidance(
            stage = "Fix error",
            nextAction = "Open advanced settings to review the error. Rotate the credential only if it may be exposed, then reconnect the agent.",
            healthy = false
        )
        !secret_configured -> ConnectorSetupGuidance(
            stage = "Credential needed",
            nextAction = "Create a credential, copy it once, and store it in the external agent configuration.",
            healthy = false
        )
        status != "ENABLED" -> ConnectorSetupGuidance(
            stage = "Disabled",
            nextAction = "Enable the connector after the credential and setup block are installed in the external agent.",
            healthy = false
        )
        health_status != "HEALTHY" -> ConnectorSetupGuidance(
            stage = "Test needed",
            nextAction = "Finish the platform setup, then send a test request. Incoming approval requests appear in Approvals.",
            healthy = false
        )
        last_used_at.isNullOrBlank() -> ConnectorSetupGuidance(
            stage = "Ready, unused",
            nextAction = "Send the first task from the connected agent. Requests that need a decision will appear in Approvals.",
            healthy = true
        )
        else -> ConnectorSetupGuidance(
            stage = "Recently used",
            nextAction = "The last authenticated webhook request was accepted. This does not prove the integration is currently online. Review task and approval status in the app.",
            healthy = true
        )
    }
}
