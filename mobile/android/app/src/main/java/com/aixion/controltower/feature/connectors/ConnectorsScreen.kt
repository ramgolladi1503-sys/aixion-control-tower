package com.aixion.controltower.feature.connectors

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
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
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
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
                title = "Configured Connectors",
                subtitle = if (state.connectors.isEmpty()) {
                    "No connector is configured yet. Pick ChatGPT, Codex, Claude/Cursor, Gemini, or Local Bridge from templates first."
                } else {
                    "Webhook agents, credentials, setup blocks, schema mappers, setup stage, and next action stay controlled here."
                }
            )
        }

        if (!state.loading && state.connectors.isEmpty()) {
            item {
                TowerPanel(elevated = true) {
                    Text("No connector configured yet", color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
                    Spacer(modifier = Modifier.height(TowerSpacing.sm))
                    Text(
                        "Start with ChatGPT Actions Bridge or Codex Agent Bridge. Create the connector, issue a credential, copy the setup block into the external agent, then test a sample payload.",
                        color = TowerTextMuted,
                        fontSize = 13.sp,
                        lineHeight = 19.sp
                    )
                }
            }
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
                StatusBadge(label = if (state.loading) "SYNCING" else "AGENT ACCESS", color = if (state.loading) RiskMedium else RiskLow)
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Text("Connectors", color = TowerTextPrimary, fontSize = 28.sp, fontWeight = FontWeight.SemiBold)
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    text = if (state.loading) "Loading connector console..." else "Connect ChatGPT, Codex, Claude, Gemini, local bridges, and custom agents without letting them bypass mobile approval.",
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
        Text("First-time setup", color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text("1. Pick a template such as ChatGPT Actions Bridge or Codex Agent Bridge.", color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
        Text("2. Create the connector from the selected template.", color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
        Text("3. Issue a credential and copy the setup block into the external agent.", color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
        Text("4. Test a sample payload before trusting live work.", color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
        Text("5. External agents submit Agent Work; they do not approve their own work.", color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
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
        Text("Templates", color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text("Pick a preset, create a connector, issue a credential, then copy the setup block into the external agent.", color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        templates.forEach { template ->
            val selected = template.id == selectedTemplateId
            OutlinedButton(onClick = { onTemplateSelected(template.id) }, modifier = Modifier.fillMaxWidth()) {
                Text(if (selected) "✓ ${template.display_name}" else template.display_name, lineHeight = 18.sp)
            }
            if (selected) {
                Text(template.description, color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
                if (template.setup_notes.isNotEmpty()) {
                    Text("Notes:", color = TowerTextPrimary, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                    template.setup_notes.forEach { note ->
                        Text("• $note", color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
                    }
                }
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
            }
        }
        Button(onClick = onCreate, enabled = templates.isNotEmpty(), modifier = Modifier.fillMaxWidth()) {
            Text("Create connector from selected template")
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

    TowerPanel(elevated = true) {
        Column(verticalArrangement = Arrangement.spacedBy(TowerSpacing.sm)) {
            Text(connector.name, color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold, lineHeight = 23.sp)
            Text("${connector.provider_label} • ${connector.connector_type} • ${connector.auth_type}", color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
            StatusBadge(connector.status, if (connector.status == "ENABLED") RiskLow else RiskBlocked)
            StatusBadge(connector.health_status, if (connector.health_status == "HEALTHY") RiskLow else RiskMedium)
            StatusBadge(if (connector.secret_configured) "Credential set" else "No credential", if (connector.secret_configured) RiskLow else RiskBlocked)
            StatusBadge("Failures ${connector.failed_auth_count}", if (connector.failed_auth_count == 0) RiskLow else RiskBlocked)
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

        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text("Actions: ${connector.allowed_actions.joinToString()}", color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
        Text("Repos: ${connector.allowed_repositories.joinToString().ifBlank { "wildcard" }}", color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
        Text("Last used: ${connector.last_used_at ?: "never"}", color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
        if (!connector.last_error.isNullOrBlank()) {
            Text("Last error: ${connector.last_error}", color = RiskBlocked, fontSize = 12.sp, lineHeight = 17.sp)
        }
        if (selectedTemplate != null) {
            Text("Selected setup preset: ${selectedTemplate.display_name}", color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
        }

        Column(verticalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            OutlinedButton(onClick = onToggle, modifier = Modifier.fillMaxWidth()) {
                Text(if (connector.status == "ENABLED") "Disable connector" else "Enable connector")
            }
            OutlinedButton(onClick = onIssueSecret, modifier = Modifier.fillMaxWidth()) {
                Text(if (connector.secret_configured) "Rotate credential" else "Issue credential")
            }
            OutlinedButton(onClick = onRevokeSecret, enabled = connector.secret_configured, modifier = Modifier.fillMaxWidth()) {
                Text("Revoke credential")
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

private fun ConnectorDto.setupGuidance(): ConnectorSetupGuidance {
    return when {
        failed_auth_count > 0 || !last_error.isNullOrBlank() -> ConnectorSetupGuidance(
            stage = "Fix error",
            nextAction = "Review the last error, rotate the credential if needed, then test the selected template payload again.",
            healthy = false
        )
        !secret_configured -> ConnectorSetupGuidance(
            stage = "Credential needed",
            nextAction = "Issue a credential, copy it once, and store it inside the external agent configuration.",
            healthy = false
        )
        status != "ENABLED" -> ConnectorSetupGuidance(
            stage = "Disabled",
            nextAction = "Enable the connector after the credential and setup block are installed in the external agent.",
            healthy = false
        )
        health_status != "HEALTHY" -> ConnectorSetupGuidance(
            stage = "Test needed",
            nextAction = "Copy the setup block into the external agent, apply the selected mapper if required, then test a sample payload.",
            healthy = false
        )
        last_used_at.isNullOrBlank() -> ConnectorSetupGuidance(
            stage = "Ready, unused",
            nextAction = "Send the first task from ChatGPT/Codex/Claude. Submitted work will appear in Agent Work and approvals will appear in Approvals.",
            healthy = true
        )
        else -> ConnectorSetupGuidance(
            stage = "Live",
            nextAction = "Connector is usable. Monitor submitted Agent Work and approve, deny, or request revision from Approvals.",
            healthy = true
        )
    }
}