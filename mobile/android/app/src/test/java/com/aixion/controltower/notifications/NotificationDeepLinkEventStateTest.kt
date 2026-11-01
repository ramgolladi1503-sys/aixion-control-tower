package com.aixion.controltower.notifications

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class NotificationDeepLinkEventStateTest {
    @Test
    fun repeatedTapWithSamePayloadGetsANewEventAndCanBeConsumed() {
        val state = NotificationDeepLinkEventState()
        val link = NotificationDeepLink.ApprovalRequest("approval_123", "notification_456")

        state.record(link)
        val first = state.current
        state.record(link)
        val second = state.current

        assertEquals(link, second.link)
        assertEquals(first.id + 1, second.id)
        state.consume(second.id)
        assertNull(state.current.link)
    }

    @Test
    fun staleConsumeCannotClearANewerNotificationTap() {
        val state = NotificationDeepLinkEventState()
        state.record(NotificationDeepLink.ApprovalRequest("first_123"))
        val staleEventId = state.current.id
        val latest = NotificationDeepLink.ApprovalRequest("second_456")
        state.record(latest)

        state.consume(staleEventId)

        assertEquals(latest, state.current.link)
    }

    @Test
    fun consumedNotificationDoesNotReplayOnSubsequentConsume() {
        val state = NotificationDeepLinkEventState()
        state.record(NotificationDeepLink.ApprovalRequest("approval_123"))
        val eventId = state.current.id

        state.consume(eventId)
        state.consume(eventId)

        assertNull(state.current.link)
    }

    @Test
    fun restoringConsumedEventDoesNotRestoreItsLinkAndKeepsIdsMonotonic() {
        val state = NotificationDeepLinkEventState()
        val consumedLink = NotificationDeepLink.ApprovalRequest("approval_old", "notification_old")
        state.restore(eventId = 8L, link = null, lastRecordedLink = consumedLink)

        val incoming = NotificationDeepLink.ApprovalRequest("approval_123", "notification_new")
        state.record(incoming)

        assertEquals(9L, state.current.id)
        assertEquals(incoming, state.current.link)
        assertEquals(incoming, state.lastRecordedLink)
    }

    @Test
    fun restoringPendingEventRetainsItForAuthenticationDeferral() {
        val state = NotificationDeepLinkEventState()
        val link = NotificationDeepLink.ApprovalRequest("approval_123", "notification_456")

        state.restore(eventId = 8L, link = link)

        assertEquals(8L, state.current.id)
        assertEquals(link, state.current.link)
    }
}
