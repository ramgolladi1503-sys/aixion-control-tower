package com.aixion.controltower.feature.command

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.ui.components.RiskBadge
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.theme.RiskBlocked
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerSurface
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
fun CommandChatScreen(
    viewModel: CommandChatViewModel = viewModel(),
    onOpenWorkOrders: () -> Unit = {}
) {
    val state by viewModel.state.collectAsState()
    var command by remember { mutableStateOf("") }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp)
            .verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(16.dp)
    ) {
        Column {
            Text(
                text = "Command",
                color = TowerTextPrimary,
                fontSize = 28.sp,
                fontWeight = FontWeight.Bold
            )
            Text(
                text = "Create a real backend Work Order from a manual instruction. Command does not approve, execute, or fake-save offline work.",
                color = TowerTextMuted,
                fontSize = 14.sp,
                lineHeight = 20.sp
            )
        }

        TowerPanel(elevated = true) {
            Text("Manual work composer", color = TowerTextPrimary, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
            Spacer(modifier = Modifier.height(TowerSpacing.sm))
            Text(
                text = "Use this when you want to manually prepare work. Use Connectors when ChatGPT/Codex/Claude should submit work into Aixion.",
                color = TowerTextMuted,
                fontSize = 13.sp,
                lineHeight = 19.sp
            )
            Text(
                text = "After creation, review the Work Order. Sensitive work still needs Approvals before execution continues.",
                color = TowerTextMuted,
                fontSize = 13.sp,
                lineHeight = 19.sp
            )
        }

        Column(
            modifier = Modifier
                .fillMaxWidth()
                .background(TowerSurface, RoundedCornerShape(22.dp))
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            Text("Project Context", color = TowerTextMuted, fontSize = 12.sp, fontWeight = FontWeight.Medium)
            if (state.projects.isEmpty()) {
                StatusBadge("Backend project required", RiskBlocked)
            } else {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    state.projects.take(3).forEach { project ->
                        StatusBadge(project.name, if (project == state.selectedProject) TowerAccent else RiskMedium)
                    }
                }
            }
            Text(
                text = "Command needs real backend project data. If project sync fails, no fake Work Order will be created.",
                color = TowerTextMuted,
                fontSize = 12.sp,
                lineHeight = 17.sp
            )
        }

        OutlinedTextField(
            value = command,
            onValueChange = { command = it },
            modifier = Modifier.fillMaxWidth(),
            minLines = 7,
            label = { Text("What work should be prepared?") },
            placeholder = { Text("Example: Improve Tradebot stale feed handling and prepare a safe work package with tests and rollback plan.") }
        )

        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            StatusBadge("Creates Work Order", TowerAccent)
            StatusBadge("No execution here", RiskMedium)
        }

        Button(
            onClick = { viewModel.createWorkOrder(command) },
            modifier = Modifier.fillMaxWidth(),
            enabled = command.isNotBlank() && !state.loading
        ) {
            Text(if (state.loading) "Creating..." else "Create Work Order")
        }

        state.message?.let { message ->
            Text(text = message, color = RiskLow, fontSize = 13.sp, lineHeight = 18.sp)
        }

        state.errorMessage?.let { error ->
            TowerPanel(elevated = true) {
                StatusBadge("NOT CREATED", RiskBlocked)
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text("Work Order creation failed", color = TowerTextPrimary, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
                Text(error, color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
            }
        }

        state.generatedWorkOrder?.let { workOrder ->
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
                Text("Work Order created", color = TowerTextMuted, fontSize = 12.sp, fontWeight = FontWeight.Medium)
                Text(workOrder.goal, color = TowerTextPrimary, fontSize = 16.sp, fontWeight = FontWeight.Bold)
                Text("Tasks", color = TowerTextMuted, fontSize = 12.sp, fontWeight = FontWeight.Medium)
                workOrder.tasks.ifEmpty { listOf("No tasks defined yet") }.forEach { task ->
                    Text("• $task", color = TowerTextPrimary, fontSize = 13.sp, lineHeight = 18.sp)
                }
                Text("Required Tests", color = TowerTextMuted, fontSize = 12.sp, fontWeight = FontWeight.Medium)
                workOrder.requiredTests.ifEmpty { listOf("No tests defined yet") }.forEach { test ->
                    Text("• $test", color = TowerTextPrimary, fontSize = 13.sp, lineHeight = 18.sp)
                }
                Text(
                    text = "Next: open Work Orders to review the prepared package. Approval and GitHub execution are separate steps.",
                    color = TowerTextMuted,
                    fontSize = 12.sp,
                    lineHeight = 17.sp
                )
                OutlinedButton(onClick = onOpenWorkOrders, modifier = Modifier.fillMaxWidth()) {
                    Text("View in Work Orders")
                }
            }
        }
    }
}