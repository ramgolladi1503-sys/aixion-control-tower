package com.aixion.controltower.feature.tests

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
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSurface
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
fun TestRunsScreen(viewModel: TestRunsViewModel = viewModel()) {
    val state by viewModel.state.collectAsState()

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        item {
            Text("Test Runs", color = TowerTextPrimary, fontSize = 28.sp, fontWeight = FontWeight.Bold)
            Text(
                text = if (state.loading) "Loading test evidence..." else "Execution evidence attached to approval requests.",
                color = TowerTextMuted,
                fontSize = 14.sp
            )
        }

        if (state.testRuns.isEmpty()) {
            item {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(TowerSurface, RoundedCornerShape(22.dp))
                        .padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    StatusBadge("No test runs yet", RiskMedium)
                    Text(
                        "Approved work should record test evidence before patch application.",
                        color = TowerTextPrimary,
                        fontSize = 14.sp
                    )
                }
            }
        }

        items(state.testRuns) { testRun ->
            val statusColor = when {
                testRun.status.contains("pass", ignoreCase = true) -> RiskLow
                testRun.status.contains("fail", ignoreCase = true) -> RiskCritical
                else -> RiskMedium
            }
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(TowerSurface, RoundedCornerShape(22.dp))
                    .padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    StatusBadge(testRun.status, statusColor)
                    StatusBadge(testRun.approvalRequestId.take(18), TowerTextMuted)
                }
                Text(testRun.command, color = TowerTextPrimary, fontSize = 16.sp, fontWeight = FontWeight.Bold)
                Text(testRun.outputSummary.ifBlank { "No output summary provided" }, color = TowerTextMuted, fontSize = 13.sp)
            }
        }
    }
}
