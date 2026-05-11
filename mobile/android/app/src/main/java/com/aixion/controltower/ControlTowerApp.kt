package com.aixion.controltower

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.feature.approvals.ApprovalDetailScreen
import com.aixion.controltower.feature.approvals.ApprovalInboxScreen
import com.aixion.controltower.feature.approvals.ApprovalsViewModel
import com.aixion.controltower.feature.audit.AuditTrailScreen
import com.aixion.controltower.feature.auth.AuthScreen
import com.aixion.controltower.feature.command.CommandChatScreen
import com.aixion.controltower.feature.diff.DiffViewerScreen
import com.aixion.controltower.feature.home.HomeScreen
import com.aixion.controltower.feature.mcp.MCPQueueScreen
import com.aixion.controltower.feature.mcp.MCPQueueViewModel
import com.aixion.controltower.feature.projects.ProjectsScreen
import com.aixion.controltower.feature.tests.TestRunsScreen
import com.aixion.controltower.feature.workorders.WorkOrdersScreen
import com.aixion.controltower.navigation.Route
import com.aixion.controltower.navigation.bottomRoutes

@Composable
fun ControlTowerApp() {
    ControlTowerTheme {
        val navController = rememberNavController()
        val approvalsViewModel: ApprovalsViewModel = viewModel()
        val mcpQueueViewModel: MCPQueueViewModel = viewModel()
        val backStackEntry = navController.currentBackStackEntryAsState().value
        val currentRoute = backStackEntry?.destination?.route ?: Route.Home.value

        Scaffold(
            containerColor = TowerBackground,
            bottomBar = {
                NavigationBar {
                    bottomRoutes.forEach { route ->
                        NavigationBarItem(
                            selected = currentRoute == route.value,
                            onClick = {
                                navController.navigate(route.value) {
                                    launchSingleTop = true
                                    popUpTo(Route.Home.value) { saveState = true }
                                    restoreState = true
                                }
                            },
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
                NavHost(navController = navController, startDestination = Route.Home.value) {
                    composable(Route.Home.value) {
                        HomeScreen(
                            onApprovalSelected = { approval ->
                                approvalsViewModel.selectApproval(approval)
                                navController.navigate(Route.ApprovalDetail.value)
                            }
                        )
                    }
                    composable(Route.Projects.value) { ProjectsScreen() }
                    composable(Route.WorkOrders.value) { WorkOrdersScreen() }
                    composable(Route.Command.value) { CommandChatScreen() }
                    composable(Route.Inbox.value) {
                        ApprovalInboxScreen(
                            viewModel = approvalsViewModel,
                            onApprovalSelected = { approval ->
                                approvalsViewModel.selectApproval(approval)
                                navController.navigate(Route.ApprovalDetail.value)
                            }
                        )
                    }
                    composable(Route.MCPQueue.value) {
                        MCPQueueScreen(
                            viewModel = mcpQueueViewModel,
                            onOpenApproval = { approvalId ->
                                approvalsViewModel.openApprovalById(approvalId)
                                navController.navigate(Route.ApprovalDetail.value)
                            }
                        )
                    }
                    composable(Route.Account.value) { AuthScreen() }
                    composable(Route.Tests.value) { TestRunsScreen() }
                    composable(Route.Audit.value) { AuditTrailScreen() }
                    composable(Route.Diff.value) { DiffViewerScreen(viewModel = approvalsViewModel) }
                    composable(Route.ApprovalDetail.value) {
                        ApprovalDetailScreen(
                            viewModel = approvalsViewModel,
                            onOpenDiff = { navController.navigate(Route.Diff.value) },
                            onDecisionSubmitted = { mcpQueueViewModel.refresh() }
                        )
                    }
                }
            }
        }
    }
}
