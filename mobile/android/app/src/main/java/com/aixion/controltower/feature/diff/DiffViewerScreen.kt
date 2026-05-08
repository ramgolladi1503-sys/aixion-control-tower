package com.aixion.controltower.feature.diff

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.ui.components.DiffBlock
import com.aixion.controltower.core.ui.components.RiskBadge
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
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
            Text("No diff selected", color = TowerTextPrimary, fontSize = 22.sp, fontWeight = FontWeight.Bold)
            Text("Select an approval request first.", color = TowerTextMuted, fontSize = 14.sp)
        }
        return
    }

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        item {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(
                    text = "Diff Viewer",
                    color = TowerTextPrimary,
                    fontSize = 28.sp,
                    fontWeight = FontWeight.Bold
                )
                Text(text = approval.title, color = TowerTextMuted, fontSize = 14.sp)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    RiskBadge(approval.risk)
                    StatusBadge("${approval.files.size} files", TowerAccent)
                }
            }
        }

        items(approval.files) { file -> DiffBlock(file = file) }
    }
}
