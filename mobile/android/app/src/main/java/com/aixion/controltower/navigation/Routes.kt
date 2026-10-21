package com.aixion.controltower.navigation

sealed class Route(val value: String, val label: String) {
    data object Auth : Route("auth", "Login")
    data object Home : Route("home", "Home")
    data object Projects : Route("projects", "Projects")
    data object Command : Route("command", "Command")
    data object WorkOrders : Route("work_orders", "Work")
    data object Inbox : Route("inbox", "Approvals")
    data object AgentTasks : Route("agent_tasks", "Agent Work")
    data object AgentRuns : Route("agent_runs", "Runs")
    data object AgentAccess : Route("agent_access", "Agent Access")
    data object Connectors : Route("connectors", "Connect")
    data object MCPQueue : Route("mcp_queue", "MCP")
    data object Tests : Route("tests", "Tests")
    data object Ops : Route("ops", "Ops")
    data object Audit : Route("audit", "Audit")
    data object Account : Route("account", "Account")
    data object ApprovalDetail : Route("approval_detail", "Approval Detail")
    data object Diff : Route("diff", "Diff")
}

/** Release-one destinations: status, approvals/history, platform setup, and account access. */
val primaryRoutes = listOf(
    Route.Home,
    Route.Inbox,
    Route.Connectors,
    Route.Account
)

/** Later-release screens stay implemented but are not exposed in the release-one navigation. */
val laterReleaseRoutes = listOf(
    Route.Projects,
    Route.WorkOrders,
    Route.Command,
    Route.AgentRuns,
    Route.AgentTasks,
    Route.AgentAccess,
    Route.MCPQueue,
    Route.Tests,
    Route.Ops,
    Route.Audit
)

val allNavigationRoutes = primaryRoutes + laterReleaseRoutes

fun primaryRouteFor(currentRoute: String): Route? = when (currentRoute) {
    Route.ApprovalDetail.value, Route.Diff.value -> Route.Inbox
    else -> primaryRoutes.firstOrNull { it.value == currentRoute }
}

fun shouldPopBackToInbox(currentRoute: String, targetRoute: Route): Boolean =
    targetRoute == Route.Inbox &&
        currentRoute in setOf(Route.ApprovalDetail.value, Route.Diff.value)
