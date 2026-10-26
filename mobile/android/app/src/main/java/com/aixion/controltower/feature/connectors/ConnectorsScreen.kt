package com.aixion.controltower.feature.connectors

import androidx.compose.foundation.background
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerDivider
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.towerScreenHorizontalPadding

internal fun providerSetupDescription(provider: String): String = when (provider) {
    "ANTIGRAVITY" -> "Antigravity's local pre-tool hook is the supported approval path. Install it on the computer running this project, and keep that computer, session, and API reachable while a request waits."
    "CODEX" -> "Codex Desktop phone approval delivery and returning a decision to the same task are not verified. This setup page does not establish a live approval connection. Do not rely on it for live work."
    else -> "Choose a supported provider and complete setup on the computer where the agent runs."
}

internal fun providerSetupStatus(provider: String): String = when (provider) {
    "CODEX" -> "Unverified · not a live connection"
    "ANTIGRAVITY" -> "Supported hook · setup required"
    else -> "Not verified"
}

internal fun providerSetupButtonLabel(provider: String): String = when (provider) {
    "ANTIGRAVITY" -> "Open Antigravity setup"
    "CODEX" -> "View setup only (unverified)"
    else -> "View setup"
}

@Composable
fun ConnectorsScreen(
    onOpenAgentAccess: (String) -> Unit = {}
) {
    ConnectorsContent(onOpenAgentAccess = onOpenAgentAccess)
}

@Composable
fun ConnectorsContent(
    onOpenAgentAccess: (String) -> Unit = {}
) {
    val palette = LocalTowerPalette.current
    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(palette.canvas)
            .padding(horizontal = towerScreenHorizontalPadding(), vertical = TowerSpacing.lg),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
    ) {
        item { ConnectorsHero() }
        item { ProviderSetupPanel(onOpenAgentAccess = onOpenAgentAccess) }
        item { ConnectionGuidanceDisclosure() }
    }
}

@Composable
private fun ConnectionGuidanceDisclosure() {
    var expanded by remember { mutableStateOf(false) }
    val palette = LocalTowerPalette.current
    TowerPanel {
        TextButton(
            onClick = { expanded = !expanded },
            modifier = Modifier
                .fillMaxWidth()
                .semantics {
                    stateDescription = if (expanded) "Expanded" else "Collapsed"
                }
        ) {
            Text(
                text = if (expanded) "Hide connection details" else "How connection works",
                color = palette.brand,
                style = MaterialTheme.typography.titleMedium
            )
        }
        AnimatedVisibility(
            visible = expanded,
            enter = fadeIn() + expandVertically(),
            exit = fadeOut() + shrinkVertically()
        ) {
            Column {
                ConnectorPrerequisitesContent()
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                ApprovalFlowNote()
            }
        }
    }
}

@Composable
private fun ConnectorPrerequisitesContent() {
    val palette = LocalTowerPalette.current
    Column {
        Text(
            "Before you connect",
            modifier = Modifier.semantics { heading() },
            color = palette.ink,
            style = MaterialTheme.typography.titleMedium
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        ConnectorStep(
            number = "1",
            title = "Open the project",
            detail = "Use the computer and agent session that should pause for approvals."
        )
        ConnectorStep(
            number = "2",
            title = "Keep the computer available",
            detail = "Keep it powered on and awake, with the agent session and Control Tower API reachable while a request is waiting."
        )
        ConnectorStep(
            number = "3",
            title = "Save your one-time token",
            detail = "Setup shows the connector token once. Store it like a password."
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            "The phone cannot wake or restart the agent computer. This screen does not verify that the provider hook is installed.",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodySmall
        )
    }
}

@Composable
private fun ConnectorStep(number: String, title: String, detail: String) {
    val palette = LocalTowerPalette.current
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(TowerSpacing.md),
        verticalAlignment = Alignment.Top
    ) {
        Text(
            text = number,
            color = palette.brand,
            style = MaterialTheme.typography.titleMedium
        )
        Column(modifier = Modifier.weight(1f)) {
            Text(title, color = palette.ink, style = MaterialTheme.typography.titleSmall)
            Text(detail, color = palette.inkMuted, style = MaterialTheme.typography.bodyMedium)
        }
    }
    Spacer(modifier = Modifier.height(TowerSpacing.md))
}

@Composable
private fun ConnectorsHero() {
    val palette = LocalTowerPalette.current
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = TowerSpacing.sm)
    ) {
        StatusBadge(label = "Set up a project", color = palette.brand, uppercase = false)
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Text(
            text = "Connect an agent",
            modifier = Modifier.semantics { heading() },
            color = palette.ink,
            style = MaterialTheme.typography.headlineLarge
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            text = "Choose the provider used by this project. Complete setup on the computer where the agent runs.",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
    }
}

@Composable
private fun ProviderSetupPanel(onOpenAgentAccess: (String) -> Unit) {
    val palette = LocalTowerPalette.current
    TowerPanel(modifier = Modifier.testTag("provider-setup-panel")) {
        Text(
            text = "Choose a provider",
            modifier = Modifier.semantics { heading() },
            color = palette.ink,
            style = MaterialTheme.typography.titleLarge
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        ProviderSetupOption(
            name = "Antigravity",
            status = providerSetupStatus("ANTIGRAVITY"),
            statusColor = palette.caution,
            description = providerSetupDescription("ANTIGRAVITY"),
            buttonLabel = providerSetupButtonLabel("ANTIGRAVITY"),
            onClick = { onOpenAgentAccess("ANTIGRAVITY") }
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        TowerDivider(modifier = Modifier.height(1.dp))
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        ProviderSetupOption(
            name = "Codex",
            status = providerSetupStatus("CODEX"),
            statusColor = palette.caution,
            description = providerSetupDescription("CODEX"),
            buttonLabel = providerSetupButtonLabel("CODEX"),
            onClick = { onOpenAgentAccess("CODEX") }
        )
    }
}

@Composable
private fun ProviderSetupOption(
    name: String,
    status: String,
    statusColor: androidx.compose.ui.graphics.Color,
    description: String,
    buttonLabel: String,
    onClick: () -> Unit
) {
    val palette = LocalTowerPalette.current
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .testTag("provider-option-${name.lowercase()}"),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.xs)
    ) {
        Text(
            text = name,
            modifier = Modifier.semantics { heading() },
            color = palette.ink,
            style = MaterialTheme.typography.titleMedium
        )
        StatusBadge(label = status, color = statusColor, uppercase = false)
        Text(
            text = description,
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        Button(onClick = onClick, modifier = Modifier.fillMaxWidth()) {
            Text(buttonLabel)
        }
    }
}

@Composable
private fun ApprovalFlowNote() {
    val palette = LocalTowerPalette.current
    Column {
        Text(
            text = "When an approval arrives",
            modifier = Modifier.semantics { heading() },
            color = palette.ink,
            style = MaterialTheme.typography.titleMedium
        )
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        Text(
            text = "The request appears in Approvals. Review the exact action, then approve or deny it. The decision returns to the same waiting task only while its computer, session, and API remain reachable.",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
    }
}
