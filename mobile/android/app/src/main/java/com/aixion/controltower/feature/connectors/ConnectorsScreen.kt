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
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary
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
    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(horizontal = towerScreenHorizontalPadding(), vertical = TowerSpacing.lg),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
    ) {
        item { ConnectorsHero() }
        item { NativeAgentAccessPanel(onOpenAgentAccess = onOpenAgentAccess) }
    }
}

@Composable
private fun ConnectorsHero() {
    TowerHeroPanel {
        Row(verticalAlignment = Alignment.Top) {
            Column(modifier = Modifier.weight(1f)) {
                StatusBadge(label = "PLATFORM SETUP", color = RiskLow)
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Text(
                    "Connect a platform",
                    color = TowerTextPrimary,
                    fontSize = 28.sp,
                    fontWeight = FontWeight.SemiBold,
                    lineHeight = 34.sp
                )
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    "Set up the coding agent running on your computer. The computer and the same agent session must stay on while a request is waiting. Antigravity is the first supported target; Codex phone approval is not verified yet.",
                    color = TowerTextMuted,
                    fontSize = 14.sp,
                    lineHeight = 21.sp
                )
                Spacer(modifier = Modifier.height(TowerSpacing.lg))
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    StatusBadge(label = "ANTIGRAVITY SETUP", color = TowerAccent)
                    StatusBadge(label = "CODEX FLOW UNVERIFIED", color = RiskMedium)
                }
            }
            Spacer(modifier = Modifier.padding(horizontal = TowerSpacing.xs))
            ForgedLogoMark(size = 44.dp, color = TowerTextPrimary.copy(alpha = 0.78f))
        }
    }
}

@Composable
private fun NativeAgentAccessPanel(onOpenAgentAccess: (String) -> Unit) {
    TowerPanel(elevated = true) {
        Text("Antigravity approval setup", color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            "Connect the local Antigravity hook and its agent credentials. Keep the computer and the same agent session running while you wait for a phone decision.",
            color = TowerTextMuted,
            fontSize = 14.sp,
            lineHeight = 21.sp
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Button(onClick = { onOpenAgentAccess("ANTIGRAVITY") }, modifier = Modifier.fillMaxWidth()) {
            Text("Set up Antigravity")
        }
    }

    TowerPanel {
        Text("Codex permission setup", color = TowerTextPrimary, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        Text(
            "A local PermissionRequest hook can be configured. Same-invocation phone approve/deny has not been verified yet.",
            color = TowerTextMuted,
            fontSize = 14.sp,
            lineHeight = 21.sp
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        OutlinedButton(onClick = { onOpenAgentAccess("CODEX") }, modifier = Modifier.fillMaxWidth()) {
            Text("Open platform setup")
        }
    }
}
