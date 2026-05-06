package com.aixion.controltower.feature.approvals

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
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.ui.components.ApprovalCard
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.theme.RiskBlocked
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary
import com.aixion.controltower.data.mock.MockData

@Composable
fun ApprovalInboxScreen() {
    val pending = MockData.approvals.filter { it.status == ApprovalStatus.PENDING_REVIEW }
    val blocked = MockData.approvals.filter { it.status == ApprovalStatus.BLOCKED }

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        item {
            Text(
                text = "Approval Inbox",
                color = TowerTextPrimary,
                fontSize = 28.sp,
                fontWeight = FontWeight.Bold
            )
            Text(
                text = "Review agent requests before code moves.",
                color = TowerTextMuted,
                fontSize = 14.sp
            )
        }

        item {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                StatusBadge("Pending ${pending.size}", RiskMedium)
                StatusBadge("Blocked ${blocked.size}", RiskBlocked)
                StatusBadge("Approved 0", RiskLow)
            }
        }

        items(MockData.approvals) { approval ->
            ApprovalCard(approval = approval)
        }
    }
}
