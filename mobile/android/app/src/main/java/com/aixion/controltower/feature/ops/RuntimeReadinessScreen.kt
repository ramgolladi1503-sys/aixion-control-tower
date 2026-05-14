package com.aixion.controltower.feature.ops

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.model.RuntimeReadinessSummary
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSurface
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
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text(
                        text = "Runtime Readiness",
                        color = TowerTextPrimary,
                        fontSize = 28.sp,
                        fontWeight = FontWeight.Bold
                    )
                    Text(
                        text = when {
                            state.loading -> "Checking backend readiness..."
                            readiness != null -> "Backend operational status from /ops/readiness."
                            else -> "Unable to load backend readiness."
                        },
                        color = TowerTextMuted,
                        fontSize = 14.sp
                    )
                }
                Button(onClick = { viewModel.refresh() }) {
                    Text("Refresh")
                }
            }
        }

        if (state.errorMessage.isNotBlank()) {
            item {
                StatusCard(
                    title = "Readiness unavailable",
                    value = state.errorMessage,
                    healthy = false
                )
            }
        }

        if (readiness != null) {
            item { SummaryCard(readiness) }
            item { StatusCard("Profile", readiness.profile, healthy = true) }
            item { StatusCard("Auth enabled", readiness.authEnabled.toString(), readiness.authEnabled) }
            item { StatusCard("Database reachable", readiness.dbReachable.toString(), readiness.dbReachable) }
            item {
                StatusCard(
                    "Migrations applied",
                    readiness.migrationsApplied.toString(),
                    readiness.migrationsApplied
                )
            }
            item {
                StatusCard(
                    "Recovery snapshot available",
                    readiness.recoverySnapshotAvailable.toString(),
                    readiness.recoverySnapshotAvailable
                )
            }
            item {
                StatusCard(
                    "GitHub token configured",
                    readiness.githubTokenConfigured.toString(),
                    readiness.githubTokenConfigured
                )
            }
            item {
                StatusCard(
                    "FCM server key configured",
                    readiness.fcmServerKeyConfigured.toString(),
                    readiness.fcmServerKeyConfigured
                )
            }
            if (readiness.missingMigrationIds.isNotEmpty()) {
                item { TextBlock("Missing migrations", readiness.missingMigrationIds.joinToString("\n")) }
            }
            if (readiness.warnings.isNotEmpty()) {
                item { TextBlock("Warnings", readiness.warnings.joinToString("\n")) }
            }
            if (readiness.errors.isNotEmpty()) {
                item { TextBlock("Errors", readiness.errors.joinToString("\n")) }
            }
        }
    }
}

@Composable
private fun SummaryCard(readiness: RuntimeReadinessSummary) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(TowerSurface, RoundedCornerShape(22.dp))
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)
    ) {
        StatusBadge(readiness.readinessLabel, if (readiness.isReady) RiskLow else RiskCritical)
        Text(
            readiness.status.uppercase(),
            color = TowerTextPrimary,
            fontSize = 20.sp,
            fontWeight = FontWeight.Bold
        )
        Text("Generated: ${readiness.generatedAt ?: "unknown"}", color = TowerTextMuted, fontSize = 13.sp)
        Text("Recovery format: ${readiness.recoveryFormatVersion}", color = TowerTextMuted, fontSize = 13.sp)
    }
}

@Composable
private fun StatusCard(title: String, value: String, healthy: Boolean) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(TowerSurface, RoundedCornerShape(18.dp))
            .padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp)
    ) {
        StatusBadge(if (healthy) "OK" else "Check", if (healthy) RiskLow else RiskCritical)
        Text(title, color = TowerTextPrimary, fontWeight = FontWeight.Bold)
        Text(value, color = TowerTextMuted, fontSize = 13.sp)
    }
}

@Composable
private fun TextBlock(title: String, body: String) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(TowerSurface, RoundedCornerShape(18.dp))
            .padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        Text(title, color = TowerAccent, fontWeight = FontWeight.Bold)
        Text(body, color = TowerTextMuted, fontSize = 13.sp)
    }
}
