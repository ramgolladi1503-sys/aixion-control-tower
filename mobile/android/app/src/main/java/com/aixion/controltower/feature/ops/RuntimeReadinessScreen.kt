package com.aixion.controltower.feature.ops

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
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.model.RuntimeReadinessSummary
import com.aixion.controltower.core.ui.components.ForgedLogoMark
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerHeroPanel
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.components.TowerSectionHeader
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
fun RuntimeReadinessScreen(viewModel: RuntimeReadinessViewModel = viewModel()) {
    val state by viewModel.state.collectAsState()
    val readiness = state.readiness

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
    ) {
        item {
            RuntimeHero(readiness = readiness, loading = state.loading, onRefresh = { viewModel.refresh() })
        }

        if (state.errorMessage.isNotBlank()) {
            item {
                RuntimeStatusCard(
                    title = "Readiness unavailable",
                    value = state.errorMessage,
                    healthy = false
                )
            }
        }

        if (readiness != null) {
            item { SummaryCard(readiness) }
            item {
                TowerSectionHeader(
                    title = "Operational Gates",
                    subtitle = "Production readiness depends on auth, database, migrations, recovery, GitHub, and FCM state."
                )
            }
            item { RuntimeStatusCard("Profile", readiness.profile, healthy = true) }
            item { RuntimeStatusCard("Auth enabled", readiness.authEnabled.toString(), readiness.authEnabled) }
            item { RuntimeStatusCard("Database reachable", readiness.dbReachable.toString(), readiness.dbReachable) }
            item { RuntimeStatusCard("Migrations applied", readiness.migrationsApplied.toString(), readiness.migrationsApplied) }
            item { RuntimeStatusCard("Recovery snapshot available", readiness.recoverySnapshotAvailable.toString(), readiness.recoverySnapshotAvailable) }
            item { RuntimeStatusCard("GitHub token configured", readiness.githubTokenConfigured.toString(), readiness.githubTokenConfigured) }
            item { RuntimeStatusCard("FCM server key configured", readiness.fcmServerKeyConfigured.toString(), readiness.fcmServerKeyConfigured) }
            if (readiness.missingMigrationIds.isNotEmpty()) {
                item { TextBlock("Missing migrations", readiness.missingMigrationIds.joinToString("\n"), RiskCritical) }
            }
            if (readiness.warnings.isNotEmpty()) {
                item { TextBlock("Warnings", readiness.warnings.joinToString("\n"), RiskMedium) }
            }
            if (readiness.errors.isNotEmpty()) {
                item { TextBlock("Errors", readiness.errors.joinToString("\n"), RiskCritical) }
            }
        }
    }
}

@Composable
private fun RuntimeHero(readiness: RuntimeReadinessSummary?, loading: Boolean, onRefresh: () -> Unit) {
    TowerHeroPanel {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.Top
        ) {
            Column(modifier = Modifier.weight(1f)) {
                StatusBadge(
                    label = when {
                        loading -> "CHECKING"
                        readiness?.isReady == true -> "READY"
                        readiness != null -> "ATTENTION"
                        else -> "UNKNOWN"
                    },
                    color = when {
                        readiness?.isReady == true -> RiskLow
                        readiness != null -> RiskCritical
                        else -> RiskMedium
                    }
                )
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Text(
                    text = "Runtime Readiness",
                    color = TowerTextPrimary,
                    fontSize = 28.sp,
                    fontWeight = FontWeight.SemiBold,
                    lineHeight = 32.sp
                )
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    text = when {
                        loading -> "Checking backend readiness..."
                        readiness != null -> "Backend operational status from /ops/readiness."
                        else -> "Unable to load backend readiness."
                    },
                    color = TowerTextMuted,
                    fontSize = 14.sp,
                    lineHeight = 20.sp
                )
            }
            ForgedLogoMark(size = 52.dp, color = TowerTextPrimary.copy(alpha = 0.78f))
        }
        Spacer(modifier = Modifier.height(TowerSpacing.lg))
        Button(onClick = onRefresh, modifier = Modifier.fillMaxWidth()) {
            Text("Refresh")
        }
    }
}

@Composable
private fun SummaryCard(readiness: RuntimeReadinessSummary) {
    TowerPanel(elevated = true) {
        StatusBadge(readiness.readinessLabel, if (readiness.isReady) RiskLow else RiskCritical)
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Text(
            readiness.status.uppercase(),
            color = TowerTextPrimary,
            fontSize = 20.sp,
            fontWeight = FontWeight.SemiBold,
            lineHeight = 25.sp
        )
        Text("Generated: ${readiness.generatedAt ?: "unknown"}", color = TowerTextMuted, fontSize = 13.sp)
        Text("Recovery format: ${readiness.recoveryFormatVersion}", color = TowerTextMuted, fontSize = 13.sp)
    }
}

@Composable
private fun RuntimeStatusCard(title: String, value: String, healthy: Boolean) {
    TowerPanel(elevated = true) {
        StatusBadge(if (healthy) "OK" else "Check", if (healthy) RiskLow else RiskCritical)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(title, color = TowerTextPrimary, fontWeight = FontWeight.SemiBold)
        Text(value, color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
    }
}

@Composable
private fun TextBlock(title: String, body: String, accent: Color) {
    TowerPanel(elevated = true) {
        Text(title, color = accent, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(body, color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
    }
}