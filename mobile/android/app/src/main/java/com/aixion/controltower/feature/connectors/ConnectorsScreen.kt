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
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.Alignment
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
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
        item { SetupJourneyPanel() }
        item { NativeAgentAccessPanel(onOpenAgentAccess = onOpenAgentAccess) }
    }
}

@Composable
private fun ConnectorsHero() {
    val palette = LocalTowerPalette.current
    TowerHeroPanel {
        Row(verticalAlignment = Alignment.Top) {
            Column(modifier = Modifier.weight(1f)) {
                StatusBadge(label = "ONE-TIME SETUP", color = palette.brand)
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Text(
                    "Approve desktop work from your phone",
                    color = palette.ink,
                    fontSize = 25.sp,
                    fontWeight = FontWeight.SemiBold,
                    lineHeight = 31.sp
                )
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    "Connect the coding agent on your computer. Keep that computer and the same agent session running while an approval is waiting.",
                    color = palette.inkMuted,
                    fontSize = 14.sp,
                    lineHeight = 21.sp
                )
            }
            Spacer(modifier = Modifier.padding(horizontal = TowerSpacing.xs))
            ForgedLogoMark(size = 44.dp, color = palette.ink.copy(alpha = 0.78f))
        }
    }
}

@Composable
private fun SetupJourneyPanel() {
    val palette = LocalTowerPalette.current
    TowerPanel {
        Text("How phone approvals work", color = palette.ink, fontSize = 17.sp, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        SetupStep(number = "1", title = "Set up the agent", detail = "Install its Control Tower hook on the computer running your project.")
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        SetupStep(number = "2", title = "Keep the session running", detail = "The computer must stay online so the same agent task can resume.")
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        SetupStep(number = "3", title = "Decide from your phone", detail = "Review the exact request, then approve or deny it in the app.")
    }
}

@Composable
private fun SetupStep(number: String, title: String, detail: String) {
    val palette = LocalTowerPalette.current
    Row(
        horizontalArrangement = Arrangement.spacedBy(TowerSpacing.sm),
        verticalAlignment = Alignment.Top
    ) {
        StatusBadge(label = number, color = palette.brand)
        Column(modifier = Modifier.weight(1f)) {
            Text(title, color = palette.ink, fontSize = 14.sp, fontWeight = FontWeight.SemiBold, lineHeight = 20.sp)
            Text(detail, color = palette.inkMuted, fontSize = 14.sp, lineHeight = 21.sp)
        }
    }
}

@Composable
private fun NativeAgentAccessPanel(onOpenAgentAccess: (String) -> Unit) {
    val palette = LocalTowerPalette.current
    TowerPanel(elevated = true) {
        StatusBadge(label = "PHONE FLOW NOT VERIFIED", color = palette.caution)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text("Antigravity", color = palette.ink, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            "Hook and connector setup is available, but returning a phone decision to the same Antigravity task has not been verified.",
            color = palette.inkMuted,
            fontSize = 14.sp,
            lineHeight = 21.sp
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Button(onClick = { onOpenAgentAccess("ANTIGRAVITY") }, modifier = Modifier.fillMaxWidth()) {
            Text("Set up Antigravity")
        }
    }

    TowerPanel {
        StatusBadge(label = "PHONE FLOW NOT VERIFIED", color = palette.caution)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text("Codex", color = palette.ink, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        Text(
            "Setup is available, but returning a phone decision to the same Codex task has not been verified.",
            color = palette.inkMuted,
            fontSize = 14.sp,
            lineHeight = 21.sp
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        OutlinedButton(onClick = { onOpenAgentAccess("CODEX") }, modifier = Modifier.fillMaxWidth()) {
            Text("View Codex setup")
        }
    }
}
