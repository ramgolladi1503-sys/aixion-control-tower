package com.aixion.controltower

import com.aixion.controltower.navigation.Route
import com.aixion.controltower.notifications.NotificationDeepLink
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class NotificationNavigationTest {
    @Test
    fun approvalNotificationOpensApprovalDetail() {
        assertEquals(
            Route.ApprovalDetail,
            releaseOneNotificationDestination(NotificationDeepLink.ApprovalRequest("approval_123"))
        )
    }

    @Test
    fun agentTaskNotificationDoesNotOpenHiddenAgentWork() {
        assertNull(
            releaseOneNotificationDestination(NotificationDeepLink.AgentTask("task_123"))
        )
    }

    @Test
    fun unknownOrMissingNotificationDoesNotNavigate() {
        assertNull(
            releaseOneNotificationDestination(NotificationDeepLink.Unknown("future", "entity_123"))
        )
        assertNull(releaseOneNotificationDestination(null))
    }
}
