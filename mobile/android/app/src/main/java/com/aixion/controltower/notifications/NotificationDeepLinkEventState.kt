package com.aixion.controltower.notifications

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue

/** Snapshot for one notification tap; event IDs distinguish repeated taps with identical payloads. */
data class NotificationDeepLinkEvent(
    val id: Long,
    val link: NotificationDeepLink?
)

/** Keeps notification taps in order until the authenticated app handles each once. */
class NotificationDeepLinkEventState {
    private var nextId = 0L
    private val pending = ArrayDeque<NotificationDeepLinkEvent>()
    var lastRecordedLink: NotificationDeepLink? = null
        private set
    var current by mutableStateOf(NotificationDeepLinkEvent(id = nextId, link = null))
        private set

    fun record(link: NotificationDeepLink) {
        nextId += 1
        lastRecordedLink = link
        pending.addLast(NotificationDeepLinkEvent(id = nextId, link = link))
        publishNext()
    }

    fun restore(
        eventId: Long,
        link: NotificationDeepLink?,
        lastRecordedLink: NotificationDeepLink? = link
    ) {
        nextId = eventId.coerceAtLeast(0L)
        this.lastRecordedLink = lastRecordedLink
        pending.clear()
        if (link != null) pending.addLast(NotificationDeepLinkEvent(id = nextId, link = link))
        publishNext()
    }

    /** Restore all deferred taps in order after activity recreation. */
    fun restorePending(
        events: List<NotificationDeepLinkEvent>,
        lastRecordedLink: NotificationDeepLink?,
        eventId: Long = 0L
    ) {
        pending.clear()
        events.forEach { event ->
            if (event.link != null && event.id >= 0L) pending.addLast(event)
        }
        nextId = maxOf(eventId.coerceAtLeast(0L), pending.maxOfOrNull { it.id } ?: 0L)
        this.lastRecordedLink = lastRecordedLink
        publishNext()
    }

    fun pendingEvents(): List<NotificationDeepLinkEvent> = pending.toList()

    fun consume(eventId: Long) {
        if (current.id == eventId && current.link != null) {
            pending.removeFirst()
            publishNext()
        }
    }

    private fun publishNext() {
        current = pending.firstOrNull() ?: NotificationDeepLinkEvent(id = nextId, link = null)
    }
}
