package com.aixion.controltower.navigation

sealed class Route(val value: String, val label: String) {
    data object Home : Route("home", "Home")
    data object Projects : Route("projects", "Projects")
    data object Command : Route("command", "Command")
    data object WorkOrders : Route("work_orders", "Work")
    data object Inbox : Route("inbox", "Review")
    data object MCPQueue : Route("mcp_queue", "MCP")
    data object Tests : Route("tests", "Tests")
    data object Audit : Route("audit", "Audit")
    data object ApprovalDetail : Route("approval_detail", "Approval Detail")
    data object Diff : Route("diff", "Diff")
}

val bottomRoutes = listOf(
    Route.Home,
    Route.WorkOrders,
    Route.Command,
    Route.Inbox,
    Route.MCPQueue,
    Route.Audit
)
