package com.aixion.controltower.feature.command

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
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
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSurface
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
fun CommandChatScreen(viewModel: CommandChatViewModel = viewModel()) {
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
                text = "Turn a rough instruction into a controlled work package. Command prepares work; it does not approve or execute it by itself.",
                color = TowerTextMuted,
                fontSize = 14.sp,
                lineHeight = 20.sp
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
                StatusBadge("Using fallback project", RiskMedium)
            } else {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    state.projects.take(3).forEach { project ->
                        StatusBadge(project.name, if (project == state.selectedProject) TowerAccent else RiskMedium)
                    }
                }
            }
            Text(
                text = "Next step after creation: review the Work Orders screen, then route sensitive work through Approval before execution.",
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
            StatusBadge("Approval comes later", RiskMedium)
        }

        Button(
            onClick = { viewModel.createWorkOrder(command) },
            modifier = Modifier.fillMaxWidth(),
            enabled = command.isNotBlank() && !state.loading
        ) {
            Text(if (state.loading) "Creating..." else "Create Work Order")
        }

        state.message?.let { message ->
            Text(text = message, color = TowerTextMuted, fontSize = 13.sp)
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
                workOrder.tasks.forEach { task ->
                    Text("• $task", color = TowerTextPrimary, fontSize = 13.sp)
                }
                Text("Required Tests", color = TowerTextMuted, fontSize = 12.sp, fontWeight = FontWeight.Medium)
                workOrder.requiredTests.forEach { test ->
                    Text("• $test", color = TowerTextPrimary, fontSize = 13.sp)
                }
                Text(
                    text = "This is a prepared work package. Approval and GitHub execution are separate steps.",
                    color = TowerTextMuted,
                    fontSize = 12.sp,
                    lineHeight = 17.sp
                )
            }
        }
    }
}
