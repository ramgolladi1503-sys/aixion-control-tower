package com.aixion.controltower.feature.connectors

import android.content.ClipData
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.api.dto.ConnectorDto
import com.aixion.controltower.core.api.dto.ConnectorTemplateDto
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.theme.RiskBlocked
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSurface
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
fun ConnectorsScreen(viewModel: ConnectorsViewModel = viewModel()) {
    val state by viewModel.state.collectAsState()
    val clipboard = LocalClipboardManager.current

    fun copy(label: String, value: String) {
        clipboard.setText(AnnotatedString(value))
        viewModel.markCopied(label)
    }

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        item {
            Text(
                text = "Connectors",
                color = TowerTextPrimary,
                fontSize = 28.sp,
                fontWeight = FontWeight.Bold
            )
            Text(
                text = if (state.loading) "Loading connector console..." else "Bring-your-own-agent control from phone.",
                color = TowerTextMuted,
                fontSize = 14.sp
            )
            if (state.notice.isNotBlank()) {
                Text(state.notice, color = RiskLow, fontSize = 13.sp, modifier = Modifier.padding(top = 8.dp))
            }
            if (state.error.isNotBlank()) {
                Text(state.error, color = RiskBlocked, fontSize = 13.sp, modifier = Modifier.padding(top = 8.dp))
            }
            if (state.lastSecret.isNotBlank()) {
                OneTimeCredentialPanel(
                    secret = state.lastSecret,
                    onCopy = { copy("One-time connector credential", state.lastSecret) },
                    onHide = viewModel::hideOneTimeSecret
                )
            }
            if (state.preview.isNotBlank()) {
                Text(state.preview, color = TowerTextMuted, fontSize = 12.sp, modifier = Modifier.padding(top = 8.dp))
            }
        }

        item {
            TemplatePanel(
                templates = state.templates,
                selectedTemplateId = state.selectedTemplateId,
                onTemplateSelected = viewModel::selectTemplate,
                onCreate = viewModel::createSelectedTemplateConnector
            )
        }

        items(state.connectors) { connector ->
            ConnectorCard(
                connector = connector,
                selectedTemplate = state.selectedTemplate,
                webhookUrl = state.webhookUrl(connector),
                setupText = state.setupText(connector, state.selectedTemplate),
                onCopyWebhook = { copy("Webhook URL", state.webhookUrl(connector)) },
                onCopySetup = { copy("Connector setup", state.setupText(connector, state.selectedTemplate)) },
                onToggle = { viewModel.toggle(connector) },
                onIssueSecret = { viewModel.issueSecret(connector) },
                onRevokeSecret = { viewModel.revokeSecret(connector) },
                onApplyMapper = { viewModel.applyTemplateMapper(connector) },
                onPreviewMapper = { viewModel.previewTemplateMapper(connector) }
            )
        }
    }
}

@Composable
private fun OneTimeCredentialPanel(
    secret: String,
    onCopy: () -> Unit,
    onHide: () -> Unit
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(top = 10.dp)
            .background(TowerSurface, RoundedCornerShape(18.dp))
            .padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        Text("One-time connector credential", color = TowerTextPrimary, fontSize = 15.sp, fontWeight = FontWeight.Bold)
        Text("Copy this now. Hide it after storing it in the external agent config.", color = RiskMedium, fontSize = 12.sp)
        Text(secret, color = TowerTextMuted, fontSize = 12.sp)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            Button(onClick = onCopy, modifier = Modifier.weight(1f)) { Text("Copy credential") }
            OutlinedButton(onClick = onHide, modifier = Modifier.weight(1f)) { Text("Hide") }
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
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(TowerSurface, RoundedCornerShape(22.dp))
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)
    ) {
        Text("Templates", color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.Bold)
        Text("Pick a preset, create a connector, issue a credential, then copy the setup block into the external agent.", color = TowerTextMuted, fontSize = 13.sp)
        templates.forEach { template ->
            val selected = template.id == selectedTemplateId
            OutlinedButton(onClick = { onTemplateSelected(template.id) }, modifier = Modifier.fillMaxWidth()) {
                Text(if (selected) "✓ ${template.display_name}" else template.display_name)
            }
            if (selected) {
                Text(template.description, color = TowerTextMuted, fontSize = 12.sp)
                if (template.setup_notes.isNotEmpty()) {
                    Text("Notes: ${template.setup_notes.joinToString(" • ")}", color = TowerTextMuted, fontSize = 12.sp)
                }
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
    setupText: String,
    onCopyWebhook: () -> Unit,
    onCopySetup: () -> Unit,
    onToggle: () -> Unit,
    onIssueSecret: () -> Unit,
    onRevokeSecret: () -> Unit,
    onApplyMapper: () -> Unit,
    onPreviewMapper: () -> Unit
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(TowerSurface, RoundedCornerShape(22.dp))
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)
    ) {
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Column(modifier = Modifier.weight(1f)) {
                Text(connector.name, color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.Bold)
                Text("${connector.provider_label} • ${connector.connector_type} • ${connector.auth_type}", color = TowerTextMuted, fontSize = 12.sp)
            }
            StatusBadge(connector.status, if (connector.status == "ENABLED") RiskLow else RiskBlocked)
        }

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(connector.health_status, if (connector.health_status == "HEALTHY") RiskLow else RiskMedium)
            StatusBadge(if (connector.secret_configured) "Credential set" else "No credential", if (connector.secret_configured) RiskLow else RiskBlocked)
            StatusBadge("Failures ${connector.failed_auth_count}", if (connector.failed_auth_count == 0) RiskLow else RiskBlocked)
        }

        Text("Webhook", color = TowerTextPrimary, fontSize = 14.sp, fontWeight = FontWeight.Bold)
        Text(webhookUrl, color = TowerTextMuted, fontSize = 12.sp)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            OutlinedButton(onClick = onCopyWebhook, modifier = Modifier.weight(1f)) { Text("Copy webhook") }
            OutlinedButton(onClick = onCopySetup, modifier = Modifier.weight(1f)) { Text("Copy setup") }
        }

        Text("Actions: ${connector.allowed_actions.joinToString()}", color = TowerTextMuted, fontSize = 12.sp)
        Text("Repos: ${connector.allowed_repositories.joinToString().ifBlank { "wildcard" }}", color = TowerTextMuted, fontSize = 12.sp)
        Text("Last used: ${connector.last_used_at ?: "never"}", color = TowerTextMuted, fontSize = 12.sp)
        if (!connector.last_error.isNullOrBlank()) {
            Text("Last error: ${connector.last_error}", color = RiskBlocked, fontSize = 12.sp)
        }
        if (selectedTemplate != null) {
            Text("Selected setup preset: ${selectedTemplate.display_name}", color = TowerTextMuted, fontSize = 12.sp)
        }

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            OutlinedButton(onClick = onToggle, modifier = Modifier.weight(1f)) {
                Text(if (connector.status == "ENABLED") "Disable" else "Enable")
            }
            OutlinedButton(onClick = onIssueSecret, modifier = Modifier.weight(1f)) {
                Text(if (connector.secret_configured) "Rotate credential" else "Issue credential")
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            OutlinedButton(onClick = onRevokeSecret, enabled = connector.secret_configured, modifier = Modifier.weight(1f)) {
                Text("Revoke credential")
            }
            OutlinedButton(onClick = onApplyMapper, enabled = selectedTemplate != null, modifier = Modifier.weight(1f)) {
                Text("Apply mapper")
            }
        }
        OutlinedButton(onClick = onPreviewMapper, enabled = selectedTemplate != null, modifier = Modifier.fillMaxWidth()) {
            Text("Test selected template payload")
        }
    }
}
