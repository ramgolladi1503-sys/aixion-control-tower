package com.aixion.controltower.notifications

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class NotificationDeepLinkTest {
    @Test
    fun parsesApprovalRequestReference() {
        val link = NotificationDeepLink.fromPayload("approval_request", "approval_123", "notification_456")
        assertEquals(NotificationDeepLink.ApprovalRequest("approval_123", "notification_456"), link)
        assertEquals(true, link.isApprovalRequest())
    }

    @Test
    fun parsesAgentTaskAndKeepsUnknownSafeEntityTypesTyped() {
        val taskLink = NotificationDeepLink.fromPayload("agent_task", "task_123")
        assertEquals(NotificationDeepLink.AgentTask("task_123"), taskLink)
        assertEquals(false, taskLink.isApprovalRequest())
        assertEquals(
            NotificationDeepLink.Unknown("future_entity", "entity_123"),
            NotificationDeepLink.fromPayload("future_entity", "entity_123")
        )
    }

    @Test
    fun rejectsMissingMalformedAndOversizedEntityIdentifiers() {
        assertNull(NotificationDeepLink.fromPayload(null, "approval_123"))
        assertNull(NotificationDeepLink.fromPayload("approval_request", null))
        assertNull(NotificationDeepLink.fromPayload("approval_request", "../secret"))
        assertNull(NotificationDeepLink.fromPayload("approval_request", "contains space"))
        assertNull(NotificationDeepLink.fromPayload("approval_request", "a".repeat(129)))
        assertNull(NotificationDeepLink.fromPayload("bad entity type", "approval_123"))
    }

    @Test
    fun ignoresMalformedOptionalNotificationIdentifierWithoutDroppingApprovalLink() {
        assertEquals(
            NotificationDeepLink.ApprovalRequest("approval_123"),
            NotificationDeepLink.fromPayload("approval_request", "approval_123", "bad/id")
        )
    }
}
