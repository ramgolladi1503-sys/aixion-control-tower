package com.aixion.controltower.feature.diff

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
import com.aixion.controltower.core.ui.components.DiffBlock
import com.aixion.controltower.core.ui.components.ForgedLogoMark
import com.aixion.controltower.core.ui.components.RiskBadge
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
import com.aixion.controltower.feature.approvals.ApprovalsViewModel

@Composable
fun DiffViewerScreen(viewModel: ApprovalsViewModel = viewModel()) {
    val state by viewModel.state.collectAsState()
    val approval = state.selectedApproval ?: state.approvals.firstOrNull()

    if (approval == null) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .background(TowerBackground)
                .padding(18.dp),
            verticalArrangement = Arrangement.Center
        ) {
            TowerPanel(elevated = true) {
                Text("No diff selected", color = TowerTextPrimary, fontSize = 22.sp, fontWeight = FontWeight.SemiBold)
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text("Select an approval request first.", color = TowerTextMuted, fontSize = 14.sp)
            }
        }
        return
    }

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
                        StatusBadge(label = "FULL DIFF REVIEW", color = RiskMedium)
                        Spacer(modifier = Modifier.height(TowerSpacing.md))
                        Text(
                            text = approval.title,
                            color = TowerTextPrimary,
                            fontSize = 25.sp,
                            fontWeight = FontWeight.SemiBold,
                            lineHeight = 30.sp
                        )
                        Spacer(modifier = Modifier.height(TowerSpacing.sm))
                        Text(
                            text = "Review every changed file before approving execution.",
                            color = TowerTextMuted,
                            fontSize = 14.sp,
                            lineHeight = 20.sp
                        )
                    }
                    ForgedLogoMark(size = 50.dp, color = TowerTextPrimary.copy(alpha = 0.78f))
                }
                Spacer(modifier = Modifier.height(TowerSpacing.lg))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    RiskBadge(approval.risk)
                    StatusBadge("${approval.files.size} files", TowerAccent)
                    StatusBadge("Review gate", RiskLow)
                }
            }
        }

        item {
            TowerSectionHeader(
                title = "Changed Files",
                subtitle = "Added lines are green, removed lines are red, unchanged/context lines stay muted."
            )
        }

        items(approval.files) { file -> DiffBlock(file = file) }
    }
}