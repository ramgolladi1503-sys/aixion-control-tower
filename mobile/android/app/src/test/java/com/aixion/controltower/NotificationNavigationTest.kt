package com.aixion.controltower

import com.aixion.controltower.navigation.AuthSessionGate
import com.aixion.controltower.navigation.Route
import com.aixion.controltower.notifications.NotificationDeepLink
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class NotificationNavigationTest {
    @Test
    fun approvalNotificationWaitsForAuthenticationAndAnyUnresolvedDecision() {
        assertFalse(AuthSessionGate.canHandleApprovalNotification(false, true, false))
        assertFalse(AuthSessionGate.canHandleApprovalNotification(true, false, false))
        assertFalse(AuthSessionGate.canHandleApprovalNotification(true, true, true))
        assertTrue(AuthSessionGate.canHandleApprovalNotification(true, true, false))
    }

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
