package com.aixion.controltower.feature.connectors

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
            if (state.error.isNotBlank()) {
                Text(state.error, color = RiskBlocked, fontSize = 13.sp, modifier = Modifier.padding(top = 8.dp))
            }
            if (state.lastSecret.isNotBlank()) {
                Text(
                    text = "One-time secret: ${state.lastSecret}",
                    color = RiskLow,
                    fontSize = 12.sp,
                    modifier = Modifier.padding(top = 8.dp)
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
        Text("Pick a preset, then create a connector from it.", color = TowerTextMuted, fontSize = 13.sp)
        templates.forEach { template ->
            val selected = template.id == selectedTemplateId
            OutlinedButton(onClick = { onTemplateSelected(template.id) }, modifier = Modifier.fillMaxWidth()) {
                Text(if (selected) "✓ ${template.display_name}" else template.display_name)
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
            StatusBadge(if (connector.secret_configured) "Secret set" else "No secret", if (connector.secret_configured) RiskLow else RiskBlocked)
            StatusBadge("Failures ${connector.failed_auth_count}", if (connector.failed_auth_count == 0) RiskLow else RiskBlocked)
        }

        Text("Actions: ${connector.allowed_actions.joinToString()}", color = TowerTextMuted, fontSize = 12.sp)
        Text("Repos: ${connector.allowed_repositories.joinToString().ifBlank { "wildcard" }}", color = TowerTextMuted, fontSize = 12.sp)
        Text("Last used: ${connector.last_used_at ?: "never"}", color = TowerTextMuted, fontSize = 12.sp)
        if (!connector.last_error.isNullOrBlank()) {
            Text("Last error: ${connector.last_error}", color = RiskBlocked, fontSize = 12.sp)
        }

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            OutlinedButton(onClick = onToggle, modifier = Modifier.weight(1f)) {
                Text(if (connector.status == "ENABLED") "Disable" else "Enable")
            }
            OutlinedButton(onClick = onIssueSecret, modifier = Modifier.weight(1f)) {
                Text(if (connector.secret_configured) "Rotate secret" else "Issue secret")
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            OutlinedButton(onClick = onRevokeSecret, enabled = connector.secret_configured, modifier = Modifier.weight(1f)) {
                Text("Revoke secret")
            }
            OutlinedButton(onClick = onApplyMapper, enabled = selectedTemplate != null, modifier = Modifier.weight(1f)) {
                Text("Apply mapper")
            }
        }
        OutlinedButton(onClick = onPreviewMapper, enabled = selectedTemplate != null, modifier = Modifier.fillMaxWidth()) {
            Text("Preview selected template payload")
        }
    }
}
