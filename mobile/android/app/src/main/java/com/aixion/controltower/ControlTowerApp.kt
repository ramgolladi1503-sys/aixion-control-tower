package com.aixion.controltower

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.IconButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.path
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.NavType
import androidx.navigation.navArgument
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.AccountCircle
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Notifications
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import com.aixion.controltower.core.ui.theme.TowerContentFrame
import com.aixion.controltower.feature.agentruns.AgentRunsScreen
import com.aixion.controltower.feature.agenttasks.AgentTasksScreen
import com.aixion.controltower.feature.agentaccess.AgentAccessScreen
import com.aixion.controltower.feature.approvals.ApprovalDetailScreen
import com.aixion.controltower.feature.approvals.ApprovalInboxScreen
import com.aixion.controltower.feature.approvals.ApprovalsViewModel
import com.aixion.controltower.feature.audit.AuditTrailScreen
import com.aixion.controltower.feature.auth.AuthScreen
import com.aixion.controltower.feature.auth.AuthViewModel
import com.aixion.controltower.feature.auth.LogoutConfirmationDialog
import com.aixion.controltower.feature.command.CommandChatScreen
import com.aixion.controltower.feature.connectors.ConnectorsScreen
import com.aixion.controltower.feature.diff.DiffViewerScreen
import com.aixion.controltower.feature.home.HomeScreen
import com.aixion.controltower.feature.mcp.MCPQueueScreen
import com.aixion.controltower.feature.mcp.MCPQueueViewModel
import com.aixion.controltower.feature.ops.RuntimeReadinessScreen
import com.aixion.controltower.feature.projects.ProjectsScreen
import com.aixion.controltower.feature.tests.TestRunsScreen
import com.aixion.controltower.feature.workorders.WorkOrdersScreen
import com.aixion.controltower.navigation.AuthSessionGate
import com.aixion.controltower.navigation.Route
import com.aixion.controltower.navigation.primaryRouteFor
import com.aixion.controltower.navigation.parentRouteFor
import com.aixion.controltower.navigation.primaryRoutes
import com.aixion.controltower.navigation.routeTitleFor
import com.aixion.controltower.navigation.shouldShowUpButton
import com.aixion.controltower.navigation.shouldPopBackToInbox
import com.aixion.controltower.notifications.NotificationDeepLink

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ControlTowerApp(
    notificationDeepLink: NotificationDeepLink? = null,
    notificationEventId: Long = 0L,
    onNotificationConsumed: (Long) -> Unit = {}
) {
    ControlTowerTheme {
        val palette = LocalTowerPalette.current
        val navController = rememberNavController()
        val authViewModel: AuthViewModel = viewModel()
        val authState by authViewModel.state.collectAsState()
        val approvalsViewModel: ApprovalsViewModel = viewModel()
        val mcpQueueViewModel: MCPQueueViewModel = viewModel()
        var showLogoutConfirmation by remember { mutableStateOf(false) }
        val backStackEntry = navController.currentBackStackEntryAsState().value
        val currentRoute = backStackEntry?.destination?.route ?: Route.Auth.value
        val showMainShell = AuthSessionGate.shouldShowMainShell(
            authenticated = authState.authenticated,
            sessionChecked = authState.sessionChecked,
            currentRoute = currentRoute
        )

        fun requestLogoutConfirmation() {
            showLogoutConfirmation = true
        }

        fun confirmLogout() {
            showLogoutConfirmation = false
            authViewModel.logout()
            navController.navigate(Route.Auth.value) {
                launchSingleTop = true
                popUpTo(0) { inclusive = true }
            }
        }

        BackHandler(
            enabled = authState.authenticated && authState.sessionChecked && currentRoute == Route.Home.value
        ) {
            requestLogoutConfirmation()
        }

        LogoutConfirmationDialog(
            visible = showLogoutConfirmation,
            onConfirm = ::confirmLogout,
            onDismiss = { showLogoutConfirmation = false }
        )

        LaunchedEffect(currentRoute, authState.authenticated, authState.sessionChecked) {
            AuthSessionGate.redirectRoute(
                currentRoute = currentRoute,
                authenticated = authState.authenticated,
                sessionChecked = authState.sessionChecked
            )?.let { target ->
                navController.navigate(target) {
                    launchSingleTop = true
                    if (target == Route.Auth.value) {
                        popUpTo(currentRoute) { inclusive = true }
                    } else {
                        popUpTo(Route.Auth.value) { inclusive = true }
                    }
                }
            }
        }

        LaunchedEffect(notificationDeepLink, notificationEventId, authState.authenticated, authState.sessionChecked) {
            if (!AuthSessionGate.canOpenProtectedRoute(authState.authenticated, authState.sessionChecked)) {
                return@LaunchedEffect
            }
            when (releaseOneNotificationDestination(notificationDeepLink)) {
                Route.ApprovalDetail -> {
                    val approvalLink = notificationDeepLink as? NotificationDeepLink.ApprovalRequest
                        ?: return@LaunchedEffect
                    approvalsViewModel.openApprovalById(approvalLink.entityId)
                    navController.navigate(Route.ApprovalDetail.value) { launchSingleTop = true }
                }
                else -> Unit
            }
            if (notificationDeepLink != null) {
                onNotificationConsumed(notificationEventId)
            }
        }

        Scaffold(
            containerColor = palette.canvas,
            topBar = {
                if (showMainShell) {
                    TopAppBar(
                        navigationIcon = {
                            if (shouldShowUpButton(currentRoute)) {
                                IconButton(
                                    onClick = {
                                        val parentRoute = parentRouteFor(currentRoute) ?: Route.Home
                                        if (!navController.popBackStack(parentRoute.value, inclusive = false)) {
                                            navController.navigate(parentRoute.value) {
                                                launchSingleTop = true
                                                popUpTo(currentRoute) { inclusive = true }
                                            }
                                        }
                                    }
                                ) {
                                    Icon(
                                        imageVector = Icons.AutoMirrored.Filled.ArrowBack,
                                        contentDescription = "Back"
                                    )
                                }
                            }
                        },
                        title = {
                            Text(
                                routeTitleFor(currentRoute),
                                style = MaterialTheme.typography.titleLarge
                            )
                        },
                        colors = TopAppBarDefaults.topAppBarColors(
                            containerColor = MaterialTheme.colorScheme.background,
                            titleContentColor = MaterialTheme.colorScheme.onBackground
                        )
                    )
                }
            },
            bottomBar = {
                if (showMainShell) {
                    NavigationBar(
                        containerColor = MaterialTheme.colorScheme.surface,
                        contentColor = MaterialTheme.colorScheme.onSurface
                    ) {
                        primaryRoutes.forEach { route ->
                            NavigationBarItem(
                                selected = primaryRouteFor(currentRoute) == route,
                                onClick = {
                                    val returnedToInbox = shouldPopBackToInbox(currentRoute, route) &&
                                        navController.popBackStack(Route.Inbox.value, inclusive = false)
                                    if (!returnedToInbox) {
                                        navController.navigate(route.value) {
                                            launchSingleTop = true
                                            popUpTo(Route.Home.value) { saveState = true }
                                            restoreState = true
                                        }
                                    }
                                },
                                label = { Text(route.label) },
                                colors = NavigationBarItemDefaults.colors(
                                    selectedIconColor = palette.onBrand,
                                    selectedTextColor = palette.brand,
                                    indicatorColor = palette.brand,
                                    unselectedIconColor = palette.inkMuted,
                                    unselectedTextColor = palette.inkMuted
                                ),
                                icon = {
                                    Icon(
                                        imageVector = primaryRouteIcon(route.value),
                                        contentDescription = null
                                    )
                                }
                            )
                        }
                    }
                }
            }
        ) { padding ->
            androidx.compose.foundation.layout.Box(
                modifier = Modifier
                    .padding(padding)
                    .background(palette.canvas)
            ) {
                NavHost(navController = navController, startDestination = Route.Auth.value) {
                    composable(Route.Auth.value) {
                        TowerContentFrame { AuthScreen(viewModel = authViewModel) }
                    }
                    composable(Route.Home.value) {
                        TowerContentFrame {
                            HomeScreen(
                                onApprovalSelected = { approval ->
                                    approvalsViewModel.selectApproval(approval)
                                    navController.navigate(Route.ApprovalDetail.value)
                                },
                                onOpenConnect = {
                                    navController.navigate(Route.Connectors.value) {
                                        launchSingleTop = true
                                    }
                                },
                                onOpenApprovals = {
                                    navController.navigate(Route.Inbox.value) {
                                        launchSingleTop = true
                                        popUpTo(Route.Home.value) { saveState = true }
                                        restoreState = true
                                    }
                                }
                            )
                        }
                    }
                    composable(Route.Projects.value) { ProjectsScreen() }
                    composable(Route.WorkOrders.value) { WorkOrdersScreen() }
                    composable(Route.Command.value) {
                        CommandChatScreen(
                            onOpenWorkOrders = {
                                navController.navigate(Route.WorkOrders.value) { launchSingleTop = true }
                            }
                        )
                    }
                    composable(Route.Inbox.value) {
                        TowerContentFrame {
                            ApprovalInboxScreen(
                                viewModel = approvalsViewModel,
                                onApprovalSelected = { approval ->
                                    approvalsViewModel.selectApproval(approval)
                                    navController.navigate(Route.ApprovalDetail.value)
                                }
                            )
                        }
                    }
                    composable(Route.AgentTasks.value) {
                        AgentTasksScreen(
                            deepLinkTaskId = (notificationDeepLink as? NotificationDeepLink.AgentTask)?.entityId,
                            onOpenApproval = { linkedApprovalId ->
                                approvalsViewModel.openApprovalById(linkedApprovalId)
                                navController.navigate(Route.ApprovalDetail.value)
                            }
                        )
                    }
                    composable(Route.AgentRuns.value) { AgentRunsScreen() }
                    composable(
                        route = Route.AgentAccess.value,
                        arguments = listOf(navArgument("provider") { type = NavType.StringType })
                    ) { TowerContentFrame { AgentAccessScreen() } }
                    composable(Route.Connectors.value) {
                        TowerContentFrame {
                            ConnectorsScreen(
                                onOpenAgentAccess = { provider ->
                                    navController.navigate(Route.AgentAccess.forProvider(provider)) {
                                        launchSingleTop = true
                                    }
                                }
                            )
                        }
                    }
                    composable(Route.MCPQueue.value) {
                        MCPQueueScreen(
                            viewModel = mcpQueueViewModel,
                            onOpenApproval = { linkedApprovalId ->
                                approvalsViewModel.openApprovalById(linkedApprovalId)
                                navController.navigate(Route.ApprovalDetail.value)
                            }
                        )
                    }
                    composable(Route.Account.value) {
                        TowerContentFrame {
                            AuthScreen(
                                viewModel = authViewModel,
                                onLogoutRequested = ::requestLogoutConfirmation
                            )
                        }
                    }
                    composable(Route.Ops.value) { RuntimeReadinessScreen() }
                    composable(Route.Tests.value) { TestRunsScreen() }
                    composable(Route.Audit.value) { AuditTrailScreen() }
                    composable(Route.Diff.value) {
                        TowerContentFrame { DiffViewerScreen(viewModel = approvalsViewModel) }
                    }
                    composable(Route.ApprovalDetail.value) {
                        TowerContentFrame {
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
}

internal fun primaryRouteIcon(route: String): ImageVector = when (route) {
    Route.Home.value -> Icons.Filled.Home
    Route.Inbox.value -> Icons.Filled.Notifications
    Route.Connectors.value -> ConnectLinkIcon
    Route.Account.value -> Icons.Filled.AccountCircle
    else -> Icons.Filled.Home
}

/** A small local link glyph keeps the Connect destination semantic without pulling in the
 * much larger optional Material extended icon artifact. */
internal val ConnectLinkIcon: ImageVector = ImageVector.Builder(
    name = "ConnectLink",
    defaultWidth = 24.dp,
    defaultHeight = 24.dp,
    viewportWidth = 24f,
    viewportHeight = 24f
).apply {
    path(fill = SolidColor(Color.Black)) {
        moveTo(3.9f, 12f)
        curveTo(3.9f, 10.84f, 4.84f, 9.9f, 6f, 9.9f)
        horizontalLineTo(10f)
        verticalLineTo(8f)
        horizontalLineTo(6f)
        curveTo(3.79f, 8f, 2f, 9.79f, 2f, 12f)
        reflectiveCurveToRelative(1.79f, 4f, 4f, 4f)
        horizontalLineToRelative(4f)
        verticalLineToRelative(-1.9f)
        horizontalLineTo(6f)
        curveTo(4.84f, 14.1f, 3.9f, 13.16f, 3.9f, 12f)
        close()
        moveTo(8f, 13f)
        horizontalLineToRelative(8f)
        verticalLineToRelative(-2f)
        horizontalLineTo(8f)
        verticalLineToRelative(2f)
        close()
        moveTo(18f, 8f)
        horizontalLineTo(14f)
        verticalLineTo(9.9f)
        horizontalLineTo(18f)
        curveTo(19.16f, 9.9f, 20.1f, 10.84f, 20.1f, 12f)
        reflectiveCurveTo(19.16f, 14.1f, 18f, 14.1f)
        horizontalLineTo(14f)
        verticalLineTo(16f)
        horizontalLineTo(18f)
        curveTo(20.21f, 16f, 22f, 14.21f, 22f, 12f)
        reflectiveCurveTo(20.21f, 8f, 18f, 8f)
        close()
    }
}.build()

internal fun releaseOneNotificationDestination(link: NotificationDeepLink?): Route? =
    when (link) {
        is NotificationDeepLink.ApprovalRequest -> Route.ApprovalDetail
        is NotificationDeepLink.AgentTask,
        is NotificationDeepLink.Unknown,
        null -> null
    }
