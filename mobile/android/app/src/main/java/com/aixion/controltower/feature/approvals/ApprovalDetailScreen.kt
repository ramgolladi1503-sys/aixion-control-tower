package com.aixion.controltower.feature.approvals

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
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.ui.components.DiffBlock
import com.aixion.controltower.core.ui.components.RiskBadge
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.theme.RiskBlocked
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSurface
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary
import com.aixion.controltower.data.mock.MockData

@Composable
fun ApprovalDetailScreen(approval: ApprovalSummary = MockData.approvals.first()) {
    val isBlocked = approval.status == ApprovalStatus.BLOCKED || approval.risk.name == "BLOCKED"
    val hasRequiredActions = approval.requiredActions.isNotEmpty()
    val canApprove = !isBlocked && !hasRequiredActions

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
                    text = "Approval Detail",
                    color = TowerTextPrimary,
                    fontSize = 28.sp,
                    fontWeight = FontWeight.Bold
                )
                Text(text = approval.title, color = TowerTextPrimary, fontSize = 19.sp, fontWeight = FontWeight.Bold)
                Text(text = approval.summary, color = TowerTextMuted, fontSize = 14.sp)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    RiskBadge(approval.risk)
                    StatusBadge(approval.status.name, TowerTextMuted)
                    StatusBadge(approval.targetBranch, TowerAccent)
                }
            }
        }

        if (isBlocked) {
            item {
                WarningPanel(
                    title = "Approval blocked",
                    body = "This request violates a hard safety rule. It cannot be approved from mobile."
                )
            }
        }

        if (hasRequiredActions) {
            item {
                RequiredActionsPanel(actions = approval.requiredActions)
            }
        }

        item {
            DetailPanel(title = "Agent", body = approval.agentName)
        }

        item {
            DetailPanel(title = "Test Plan", body = approval.testPlan.ifEmpty { listOf("No tests listed") }.joinToString("\n"))
        }

        item {
            DetailPanel(title = "Rollback Plan", body = approval.rollbackPlan.ifBlank { "No rollback plan provided" })
        }

        item {
            Text(
                text = "File Changes",
                color = TowerTextPrimary,
                fontSize = 18.sp,
                fontWeight = FontWeight.Bold
            )
        }

        items(approval.files) { file ->
            DiffBlock(file = file)
        }

        item {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(
                    onClick = { },
                    enabled = canApprove,
                    colors = ButtonDefaults.buttonColors(containerColor = RiskLow),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text("Approve")
                }
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
                    OutlinedButton(onClick = { }, modifier = Modifier.weight(1f)) {
                        Text("Reject")
                    }
                    OutlinedButton(onClick = { }, modifier = Modifier.weight(1f)) {
                        Text("Request Revision")
                    }
                }
                if (!canApprove) {
                    Text(
                        text = "Approval disabled until safety requirements are satisfied.",
                        color = RiskMedium,
                        fontSize = 13.sp
                    )
                }
            }
        }
    }
}

@Composable
private fun DetailPanel(title: String, body: String) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(TowerSurface, RoundedCornerShape(22.dp))
            .padding(16.dp)
    ) {
        Text(title, color = TowerTextMuted, fontSize = 12.sp, fontWeight = FontWeight.Medium)
        Spacer(modifier = Modifier.height(8.dp))
        Text(body, color = TowerTextPrimary, fontSize = 14.sp)
    }
}

@Composable
private fun WarningPanel(title: String, body: String) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(RiskBlocked.copy(alpha = 0.16f), RoundedCornerShape(22.dp))
            .padding(16.dp)
    ) {
        Text(title, color = RiskBlocked, fontSize = 15.sp, fontWeight = FontWeight.Bold)
        Spacer(modifier = Modifier.height(8.dp))
        Text(body, color = TowerTextPrimary, fontSize = 14.sp)
    }
}

@Composable
private fun RequiredActionsPanel(actions: List<String>) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(RiskCritical.copy(alpha = 0.14f), RoundedCornerShape(22.dp))
            .padding(16.dp)
    ) {
        Text("Required Before Approval", color = RiskCritical, fontSize = 15.sp, fontWeight = FontWeight.Bold)
        Spacer(modifier = Modifier.height(8.dp))
        actions.forEach { action ->
            Text("• $action", color = TowerTextPrimary, fontSize = 14.sp)
        }
    }
}
