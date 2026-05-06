package com.aixion.controltower.navigation

sealed class Route(val value: String, val label: String) {
    data object Home : Route("home", "Home")
    data object Projects : Route("projects", "Projects")
    data object Command : Route("command", "Command")
    data object Inbox : Route("inbox", "Inbox")
    data object Audit : Route("audit", "Audit")
}

val bottomRoutes = listOf(
    Route.Home,
    Route.Projects,
    Route.Command,
    Route.Inbox,
    Route.Audit
)
