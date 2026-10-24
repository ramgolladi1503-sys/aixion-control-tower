package com.aixion.controltower.notifications

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class NotificationDeepLinkEventStateTest {
    @Test
    fun repeatedTapWithSamePayloadIsQueuedAsASeparateEvent() {
        val state = NotificationDeepLinkEventState()
        val link = NotificationDeepLink.ApprovalRequest("approval_123", "notification_456")

        state.record(link)
        val first = state.current
        state.record(link)
        val second = state.current

        assertEquals(first, second)
        assertEquals(2, state.pendingEvents().size)
        state.consume(first.id)
        assertEquals(link, state.current.link)
        assertEquals(first.id + 1, state.current.id)
        state.consume(state.current.id)
        assertNull(state.current.link)
    }

    @Test
    fun repeatedStaleConsumeCannotClearTheNextQueuedNotificationTap() {
        val state = NotificationDeepLinkEventState()
        state.record(NotificationDeepLink.ApprovalRequest("first_123"))
        val staleEventId = state.current.id
        val latest = NotificationDeepLink.ApprovalRequest("second_456")
        state.record(latest)

        state.consume(staleEventId)
        assertEquals(latest, state.current.link)
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

    @Test
    fun queuedTapsSurviveActivityStateRestorationInOrder() {
        val first = NotificationDeepLink.ApprovalRequest("approval_first", "notification_first")
        val second = NotificationDeepLink.ApprovalRequest("approval_second", "notification_second")
        val original = NotificationDeepLinkEventState().apply {
            record(first)
            record(second)
        }
        val restored = NotificationDeepLinkEventState()
        restored.restorePending(
            events = original.pendingEvents(),
            lastRecordedLink = second,
            eventId = original.current.id
        )

        assertEquals(first, restored.current.link)
        restored.consume(restored.current.id)
        assertEquals(second, restored.current.link)
        restored.consume(restored.current.id)
        assertNull(restored.current.link)
    }

    @Test
    fun invalidRestoredPayloadIsNotAddedToPendingQueue() {
        val state = NotificationDeepLinkEventState()
        state.restorePending(
            events = listOf(NotificationDeepLinkEvent(1L, null)),
            lastRecordedLink = null,
            eventId = 1L
        )

        assertNull(state.current.link)
        assertEquals(0, state.pendingEvents().size)
    }
}
