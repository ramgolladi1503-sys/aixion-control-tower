package com.aixion.controltower.notifications

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue

/** Snapshot for one notification tap; event IDs distinguish repeated taps with identical payloads. */
data class NotificationDeepLinkEvent(
    val id: Long,
    val link: NotificationDeepLink?
)

/** Keeps the latest tap until the authenticated app has handled it exactly once. */
class NotificationDeepLinkEventState {
    private var nextId = 0L
    var lastRecordedLink: NotificationDeepLink? = null
        private set
    var current by mutableStateOf(NotificationDeepLinkEvent(id = nextId, link = null))
        private set

    fun record(link: NotificationDeepLink) {
        nextId += 1
        lastRecordedLink = link
        current = NotificationDeepLinkEvent(id = nextId, link = link)
    }

    fun restore(
        eventId: Long,
        link: NotificationDeepLink?,
        lastRecordedLink: NotificationDeepLink? = link
    ) {
        nextId = eventId.coerceAtLeast(0L)
        this.lastRecordedLink = lastRecordedLink
        current = NotificationDeepLinkEvent(id = nextId, link = link)
    }

    fun consume(eventId: Long) {
        if (current.id == eventId && current.link != null) {
            current = current.copy(link = null)
        }
    }
}
