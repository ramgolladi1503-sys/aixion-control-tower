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
        item { ProviderSetupPanel(onOpenAgentAccess = onOpenAgentAccess) }
        item { ApprovalFlowNote() }
    }
}

@Composable
private fun ConnectorsHero() {
    val palette = LocalTowerPalette.current
    TowerHeroPanel {
        Row(verticalAlignment = Alignment.Top) {
            Column(modifier = Modifier.weight(1f)) {
                StatusBadge(label = "SET UP A PROJECT", color = palette.brand)
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Text(
                    text = "Connect your coding agent",
                    color = palette.ink,
                    style = MaterialTheme.typography.headlineMedium
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
            color = palette.ink,
            style = MaterialTheme.typography.titleLarge
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        ProviderSetupCard(
            name = "Antigravity",
            description = "Install the Control Tower tool hook in this Antigravity project workspace.",
            buttonLabel = "Set up Antigravity",
            onClick = { onOpenAgentAccess("ANTIGRAVITY") }
        )
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        ProviderSetupCard(
            name = "Codex",
            description = "Capture local Codex permission prompts. Codex Desktop approval interception is not verified.",
            buttonLabel = "Set up Codex",
            onClick = { onOpenAgentAccess("CODEX") }
        )
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        StatusBadge(
            label = "PHONE RETURN NOT VERIFIED",
            color = palette.caution,
            uppercase = false
        )
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        Text(
            text = "Setup is available. The same-session phone approval flow still needs live verification.",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodySmall
        )
    }
}

@Composable
private fun ProviderSetupCard(
    name: String,
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
            text = "When an approval is needed",
            color = palette.ink,
            style = MaterialTheme.typography.titleMedium
        )
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        Text(
            text = "The request appears here. Review the exact action and approve or deny it. Keep the computer and agent session running so the same task can continue.",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
    }
}
