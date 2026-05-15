package com.aixion.controltower.feature.audit

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
import com.aixion.controltower.core.ui.components.ForgedLogoMark
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerHeroPanel
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.components.TowerSectionHeader
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
fun AuditTrailScreen(viewModel: AuditTrailViewModel = viewModel()) {
    val state by viewModel.state.collectAsState()

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
    ) {
        item {
            TowerHeroPanel {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.Top
                ) {
                    Column(modifier = Modifier.weight(1f)) {
                        StatusBadge(label = if (state.loading) "SYNCING" else "TRACEABLE", color = if (state.loading) RiskMedium else RiskLow)
                        Spacer(modifier = Modifier.height(TowerSpacing.md))
                        Text(
                            text = "Audit Trail",
                            color = TowerTextPrimary,
                            fontSize = 28.sp,
                            fontWeight = FontWeight.SemiBold,
                            lineHeight = 32.sp
                        )
                        Spacer(modifier = Modifier.height(TowerSpacing.sm))
                        Text(
                            text = if (state.loading) "Loading audit trail..." else "Trace every agent action and human decision.",
                            color = TowerTextMuted,
                            fontSize = 14.sp,
                            lineHeight = 20.sp
                        )
                    }
                    ForgedLogoMark(size = 52.dp, color = TowerTextPrimary.copy(alpha = 0.78f))
                }
                Spacer(modifier = Modifier.height(TowerSpacing.lg))
                StatusBadge(label = "EVENTS ${state.events.size}", color = TowerAccent)
            }
        }

        item {
            TowerSectionHeader(
                title = "Decision Evidence",
                subtitle = "Approvals, rejections, execution events, and operator actions should leave a clear record."
            )
        }

        if (!state.loading && state.events.isEmpty()) {
            item {
                TowerPanel(elevated = true) {
                    Text("No audit events yet.", color = TowerTextPrimary, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
                    Spacer(modifier = Modifier.height(TowerSpacing.sm))
                    Text("Activity will appear here as agents, approvals, and execution workers move through the system.", color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
                }
            }
        }

        items(state.events) { event ->
            TowerPanel(elevated = true) {
                StatusBadge(event.eventType, TowerAccent)
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(event.details, color = TowerTextPrimary, fontSize = 16.sp, fontWeight = FontWeight.SemiBold, lineHeight = 22.sp)
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text("${event.actor} • ${event.timestamp}", color = TowerTextMuted, fontSize = 13.sp)
            }
        }
    }
}