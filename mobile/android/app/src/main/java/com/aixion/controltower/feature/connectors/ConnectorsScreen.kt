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
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.ui.components.ForgedLogoMark
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerHeroPanel
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.towerScreenHorizontalPadding

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
            modifier = Modifier.fillMaxWidth()
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
            title = "Open the project on the agent computer",
            detail = "Use the computer and agent session that should pause for approvals."
        )
        ConnectorStep(
            number = "2",
            title = "Keep it reachable",
            detail = "The agent computer, agent session, and Control Tower API must stay available while a request is waiting."
        )
        ConnectorStep(
            number = "3",
            title = "Save the one-time token",
            detail = "Setup shows the connector token once. Store it like a password."
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            "The phone cannot wake or restart the agent computer. This screen does not verify hook installation.",
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
    TowerHeroPanel {
        Row(verticalAlignment = Alignment.Top) {
            Column(modifier = Modifier.weight(1f)) {
                StatusBadge(label = "Set up a project", color = palette.brand, uppercase = false)
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Text(
                    text = "Connect your coding agent",
                    modifier = Modifier.semantics { heading() },
                    color = palette.ink,
                    style = MaterialTheme.typography.headlineLarge
                )
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    text = "Choose the agent used by this project. Set up each project workspace on the computer where the agent runs.",
                    color = palette.inkMuted,
                    style = MaterialTheme.typography.bodyMedium
                )
            }
            Spacer(modifier = Modifier.padding(horizontal = TowerSpacing.xs))
            ForgedLogoMark(size = 44.dp, color = palette.ink.copy(alpha = 0.78f))
        }
    }
}

@Composable
private fun ProviderSetupPanel(onOpenAgentAccess: (String) -> Unit) {
    val palette = LocalTowerPalette.current
    TowerPanel {
        Text(
            text = "Choose a provider",
            modifier = Modifier.semantics { heading() },
            color = palette.ink,
            style = MaterialTheme.typography.titleLarge
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        ProviderSetupCard(
            name = "Antigravity",
            status = "Not verified",
            statusColor = palette.caution,
            description = "A local phone approval flow passed on an earlier build, but this build has not been tested on a phone. Approval delivery while away from the Mac is also unverified.",
            buttonLabel = "Set up Antigravity",
            onClick = { onOpenAgentAccess("ANTIGRAVITY") }
        )
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        ProviderSetupCard(
            name = "Codex",
            status = "Codex Desktop not verified",
            statusColor = palette.caution,
            description = "Phone approvals from Codex Desktop and returning a decision to its same task have not been verified. Do not rely on Codex Desktop approvals yet.",
            buttonLabel = "Set up Codex",
            onClick = { onOpenAgentAccess("CODEX") }
        )
    }
}

@Composable
private fun ProviderSetupCard(
    name: String,
    status: String,
    statusColor: androidx.compose.ui.graphics.Color,
    description: String,
    buttonLabel: String,
    onClick: () -> Unit
) {
    val palette = LocalTowerPalette.current
    TowerPanel(elevated = true) {
        Text(
            text = name,
            modifier = Modifier.semantics { heading() },
            color = palette.ink,
            style = MaterialTheme.typography.titleMedium
        )
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        StatusBadge(label = status, color = statusColor, uppercase = false)
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        Text(
            text = description,
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
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
            color = palette.ink,
            style = MaterialTheme.typography.titleMedium
        )
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        Text(
            text = "The request appears in Approvals. Review the exact action, then approve or deny it. Your decision returns to the waiting task only while its agent computer, session, and API remain reachable.",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
    }
}
