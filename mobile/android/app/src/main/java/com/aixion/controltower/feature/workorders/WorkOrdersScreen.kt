package com.aixion.controltower.feature.workorders

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
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.ui.components.RiskBadge
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerSurface
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
fun WorkOrdersScreen(viewModel: WorkOrdersViewModel = viewModel()) {
    val state by viewModel.state.collectAsState()

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        item {
            Text("Work Orders", color = TowerTextPrimary, fontSize = 28.sp, fontWeight = FontWeight.Bold)
            Text(
                text = when {
                    state.loading -> "Loading work orders..."
                    state.hasError -> "Backend work-order sync failed. No mock work orders are shown."
                    else -> "Structured execution packages created from commands."
                },
                color = TowerTextMuted,
                fontSize = 14.sp
            )
        }

        state.errorMessage?.let { message ->
            item {
                TowerPanel(elevated = true) {
                    StatusBadge("REAL DATA REQUIRED", RiskCritical)
                    Spacer(modifier = Modifier.height(TowerSpacing.sm))
                    Text(
                        text = "Work-order data unavailable",
                        color = TowerTextPrimary,
                        fontSize = 16.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        text = message,
                        color = TowerTextMuted,
                        fontSize = 13.sp,
                        lineHeight = 19.sp
                    )
                    Spacer(modifier = Modifier.height(TowerSpacing.md))
                    Button(onClick = viewModel::refresh, enabled = !state.loading) {
                        Text("Retry work orders")
                    }
                }
            }
        }

        if (state.hasError) {
            item {
                TowerPanel(elevated = true) {
                    Text(
                        text = "No fallback work orders loaded",
                        color = TowerTextPrimary,
                        fontSize = 16.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        text = "Authenticated work-order screens now require real backend data instead of silently rendering demo work orders. Use Retry after the backend is reachable.",
                        color = TowerTextMuted,
                        fontSize = 13.sp,
                        lineHeight = 19.sp
                    )
                }
            }
        } else {
            items(state.workOrders) { workOrder ->
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(TowerSurface, RoundedCornerShape(22.dp))
                        .padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        RiskBadge(workOrder.risk)
                        StatusBadge(workOrder.projectName, TowerAccent)
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        StatusBadge(
                            workOrder.sourceBadgeLabel,
                            if (workOrder.verifiedSource) RiskLow else TowerAccent
                        )
                        StatusBadge(workOrder.sourceLabel, TowerAccent)
                    }
                    if (workOrder.sourceDetail.isNotBlank()) {
                        Text(
                            text = workOrder.sourceDetail,
                            color = TowerTextMuted,
                            fontSize = 12.sp,
                            lineHeight = 17.sp
                        )
                    }
                    Text(workOrder.goal, color = TowerTextPrimary, fontSize = 17.sp, fontWeight = FontWeight.Bold)
                    Text("Tasks", color = TowerTextMuted, fontSize = 12.sp, fontWeight = FontWeight.Medium)
                    workOrder.tasks.forEach { task ->
                        Text("• $task", color = TowerTextPrimary, fontSize = 13.sp)
                    }
                    Text("Required Tests", color = TowerTextMuted, fontSize = 12.sp, fontWeight = FontWeight.Medium)
                    workOrder.requiredTests.ifEmpty { listOf("No tests defined yet") }.forEach { test ->
                        Text("• $test", color = TowerTextPrimary, fontSize = 13.sp)
                    }
                }
            }
        }
    }
}