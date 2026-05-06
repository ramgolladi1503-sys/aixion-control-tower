package com.aixion.controltower

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.feature.approvals.ApprovalDetailScreen
import com.aixion.controltower.feature.approvals.ApprovalInboxScreen
import com.aixion.controltower.feature.approvals.ApprovalsViewModel
import com.aixion.controltower.feature.audit.AuditTrailScreen
import com.aixion.controltower.feature.command.CommandChatScreen
import com.aixion.controltower.feature.diff.DiffViewerScreen
import com.aixion.controltower.feature.home.HomeScreen
import com.aixion.controltower.feature.projects.ProjectsScreen
import com.aixion.controltower.navigation.Route
import com.aixion.controltower.navigation.bottomRoutes

@Composable
fun ControlTowerApp() {
    ControlTowerTheme {
        var currentRoute by remember { mutableStateOf<Route>(Route.Home) }
        val approvalsViewModel: ApprovalsViewModel = viewModel()

        Scaffold(
            containerColor = TowerBackground,
            bottomBar = {
                NavigationBar {
                    bottomRoutes.forEach { route ->
                        NavigationBarItem(
                            selected = currentRoute == route,
                            onClick = { currentRoute = route },
                            label = { Text(route.label) },
                            icon = { Text(route.label.take(1)) }
                        )
                    }
                }
            }
        ) { padding ->
            androidx.compose.foundation.layout.Box(
                modifier = Modifier
                    .padding(padding)
                    .background(TowerBackground)
            ) {
                when (currentRoute) {
                    Route.Home -> HomeScreen(
                        onApprovalSelected = { approval ->
                            approvalsViewModel.selectApproval(approval)
                            currentRoute = Route.ApprovalDetail
                        }
                    )
                    Route.Projects -> ProjectsScreen()
                    Route.Command -> CommandChatScreen()
                    Route.Inbox -> ApprovalInboxScreen(
                        viewModel = approvalsViewModel,
                        onApprovalSelected = { approval ->
                            approvalsViewModel.selectApproval(approval)
                            currentRoute = Route.ApprovalDetail
                        }
                    )
                    Route.Audit -> AuditTrailScreen()
                    Route.Diff -> DiffViewerScreen(viewModel = approvalsViewModel)
                    Route.ApprovalDetail -> ApprovalDetailScreen(
                        viewModel = approvalsViewModel,
                        onOpenDiff = { currentRoute = Route.Diff }
                    )
                }
            }
        }
    }
}
