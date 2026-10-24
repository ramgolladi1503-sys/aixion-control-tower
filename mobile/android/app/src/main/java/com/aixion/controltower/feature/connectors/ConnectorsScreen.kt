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
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
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
        item { ConnectorPrerequisitesPanel() }
        item { ProviderSetupPanel(onOpenAgentAccess = onOpenAgentAccess) }
        item { ApprovalFlowNote() }
    }
}

@Composable
private fun ConnectorPrerequisitesPanel() {
    val palette = LocalTowerPalette.current
    TowerPanel {
        Text("Before you connect", color = palette.ink, style = MaterialTheme.typography.titleMedium)
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        Text(
            "Open the project on the Mac where your agent runs. Keep that Mac and the same agent session running while an approval is pending.",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        Text(
            "Choose a provider below and follow its project setup steps. The connector token appears once; save it like a password.",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        Text(
            "The Mac, agent session, and Control Tower API must stay reachable for a decision to return. The phone cannot wake or restart the Mac, and this screen cannot confirm hook installation.",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
    }
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
                    color = palette.ink,
                    style = MaterialTheme.typography.headlineLarge
                )
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    text = "Choose the agent used by this project. Set up each project workspace on the Mac where the agent runs.",
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
            color = palette.ink,
            style = MaterialTheme.typography.titleLarge
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        ProviderSetupCard(
            name = "Antigravity",
            status = "Local E2E on prior revision",
            statusColor = palette.brand,
            description = "A local PostgreSQL phone round-trip is documented for revision 2ad22d4. This app build has not been re-tested on a phone; remote delivery remains unverified.",
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
    TowerPanel {
        Text(
            text = "When an approval arrives",
            color = palette.ink,
            style = MaterialTheme.typography.titleMedium
        )
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        Text(
            text = "The request appears in Approvals. Review the exact action, then approve or deny it. Your decision returns to the waiting task only while its Mac, agent session, and API remain reachable.",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
    }
}
