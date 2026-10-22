package com.aixion.controltower.navigation

import com.aixion.controltower.ConnectLinkIcon
import com.aixion.controltower.primaryRouteIcon
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class NavigationDestinationsTest {
    @Test
    fun releaseOneNavigationHasOnlyCoreUserDestinations() {
        assertEquals(
            listOf(Route.Home, Route.Inbox, Route.Connectors, Route.Account),
            primaryRoutes
        )
    }

    @Test
    fun laterReleaseDestinationsAreNotExposedInPrimaryNavigation() {
        assertEquals(
            listOf(
                Route.Projects, Route.WorkOrders, Route.Command, Route.AgentRuns,
                Route.AgentTasks, Route.AgentAccess, Route.MCPQueue, Route.Tests, Route.Ops, Route.Audit
            ),
            laterReleaseRoutes
        )
        assertEquals(primaryRoutes + laterReleaseRoutes, allNavigationRoutes)
    }

    @Test
    fun detailDestinationsSelectApprovalsInBottomNavigation() {
        assertEquals(Route.Inbox, primaryRouteFor(Route.ApprovalDetail.value))
        assertEquals(Route.Inbox, primaryRouteFor(Route.Diff.value))
        assertEquals(Route.Connectors, primaryRouteFor(Route.AgentAccess.value))
        assertNull(primaryRouteFor(Route.Command.value))
    }

    @Test
    fun protectedDetailRoutesHaveSpecificPageTitles() {
        assertEquals("Approval Detail", routeTitleFor(Route.ApprovalDetail.value))
        assertEquals("File changes", routeTitleFor(Route.Diff.value))
        assertEquals("Agent Access", routeTitleFor("agent_access/antigravity"))
    }

    @Test
    fun connectDestinationUsesLinkIcon() {
        assertEquals(ConnectLinkIcon, primaryRouteIcon(Route.Connectors.value))
        assertEquals("ConnectLink", primaryRouteIcon(Route.Connectors.value).name)
    }

    @Test
    fun approvalsTabReturnsFromApprovalDetailAndDiff() {
        assertEquals(true, shouldPopBackToInbox(Route.ApprovalDetail.value, Route.Inbox))
        assertEquals(true, shouldPopBackToInbox(Route.Diff.value, Route.Inbox))
    }

    @Test
    fun approvalsTabDoesNotPopUnrelatedRoutes() {
        assertEquals(false, shouldPopBackToInbox(Route.Home.value, Route.Inbox))
        assertEquals(false, shouldPopBackToInbox(Route.ApprovalDetail.value, Route.Home))
    }
}
