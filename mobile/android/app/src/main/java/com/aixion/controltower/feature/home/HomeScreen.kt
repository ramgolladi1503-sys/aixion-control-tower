package com.aixion.controltower.feature.home

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
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.ui.components.ApprovalCard
import com.aixion.controltower.core.ui.components.StatusCard
import com.aixion.controltower.core.ui.theme.RiskBlocked
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary
import com.aixion.controltower.data.mock.MockData

@Composable
fun HomeScreen() {
    val pending = MockData.approvals.count { it.status == ApprovalStatus.PENDING_REVIEW }
    val blocked = MockData.approvals.count { it.status == ApprovalStatus.BLOCKED }
    val failedTests = 0

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp)
    ) {
        item {
            Column {
                Text(
                    text = "Control Tower",
                    color = TowerTextPrimary,
                    fontSize = 28.sp,
                    fontWeight = FontWeight.Bold
                )
                Text(
                    text = "AI-agent work that needs your attention.",
                    color = TowerTextMuted,
                    fontSize = 14.sp
                )
            }
        }

        item {
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                StatusCard(
                    title = "Pending",
                    value = pending.toString(),
                    subtitle = "approvals waiting",
                    accent = TowerAccent,
                    modifier = Modifier.weight(1f)
                )
                StatusCard(
                    title = "Blocked",
                    value = blocked.toString(),
                    subtitle = "policy stopped",
                    accent = RiskBlocked,
                    modifier = Modifier.weight(1f)
                )
            }
        }

        item {
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                StatusCard(
                    title = "Projects",
                    value = MockData.projects.size.toString(),
                    subtitle = "active systems",
                    accent = RiskLow,
                    modifier = Modifier.weight(1f)
                )
                StatusCard(
                    title = "Failed Tests",
                    value = failedTests.toString(),
                    subtitle = "need review",
                    accent = RiskCritical,
                    modifier = Modifier.weight(1f)
                )
            }
        }

        item {
            Spacer(modifier = Modifier.height(4.dp))
            Text(
                text = "Action Required",
                color = TowerTextPrimary,
                fontSize = 18.sp,
                fontWeight = FontWeight.Bold
            )
        }

        items(MockData.approvals.take(3)) { approval ->
            ApprovalCard(approval = approval)
        }
    }
}
