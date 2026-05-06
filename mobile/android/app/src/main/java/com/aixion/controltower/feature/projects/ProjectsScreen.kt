package com.aixion.controltower.feature.projects

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
import com.aixion.controltower.core.ui.theme.RiskBlocked
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSurface
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
fun ProjectsScreen(viewModel: ProjectsViewModel = viewModel()) {
    val state by viewModel.state.collectAsState()

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        item {
            Text(
                text = "Projects",
                color = TowerTextPrimary,
                fontSize = 28.sp,
                fontWeight = FontWeight.Bold
            )
            Text(
                text = if (state.loading) "Loading projects..." else "Execution systems under AI-agent control.",
                color = TowerTextMuted,
                fontSize = 14.sp
            )
        }

        items(state.projects) { project ->
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(TowerSurface, RoundedCornerShape(22.dp))
                    .padding(16.dp)
            ) {
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Text(project.name, color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.Bold)
                    StatusBadge(label = project.mode, color = RiskMedium)
                }
                Text(project.description, color = TowerTextMuted, fontSize = 13.sp, modifier = Modifier.padding(top = 6.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.padding(top = 12.dp)) {
                    StatusBadge("${project.pendingApprovals} pending", RiskMedium)
                    StatusBadge("${project.blockedRequests} blocked", RiskBlocked)
                    StatusBadge("Health ${project.healthScore}", RiskLow)
                }
            }
        }
    }
}
