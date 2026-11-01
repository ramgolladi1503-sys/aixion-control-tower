package com.aixion.controltower

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import com.aixion.controltower.notifications.NotificationDeepLink
import com.aixion.controltower.notifications.NotificationDeepLinkEvent
import com.aixion.controltower.notifications.NotificationDeepLinkEventState

class MainActivity : ComponentActivity() {
    private val notificationEventState = NotificationDeepLinkEventState()

    private companion object {
        const val STATE_HAS_NOTIFICATION_EVENT = "notification_event_present"
        const val STATE_NOTIFICATION_EVENT_ID = "notification_event_id"
        const val STATE_NOTIFICATION_ENTITY_TYPE = "notification_entity_type"
        const val STATE_NOTIFICATION_ENTITY_ID = "notification_entity_id"
        const val STATE_NOTIFICATION_ID = "notification_id"
        const val STATE_LAST_ENTITY_TYPE = "last_notification_entity_type"
        const val STATE_LAST_ENTITY_ID = "last_notification_entity_id"
        const val STATE_LAST_NOTIFICATION_ID = "last_notification_id"
        const val STATE_PENDING_NOTIFICATION_EVENTS = "pending_notification_events"
    }

    private fun recordNotificationIntent(intent: Intent?) {
        val link = NotificationDeepLink.fromIntent(intent) ?: return
        notificationEventState.record(link)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (savedInstanceState?.getBoolean(STATE_HAS_NOTIFICATION_EVENT) == true) {
            val type = savedInstanceState.getString(STATE_NOTIFICATION_ENTITY_TYPE)
            val entityId = savedInstanceState.getString(STATE_NOTIFICATION_ENTITY_ID)
            val notificationId = savedInstanceState.getString(STATE_NOTIFICATION_ID)
            val lastLink = NotificationDeepLink.fromPayload(
                savedInstanceState.getString(STATE_LAST_ENTITY_TYPE),
                savedInstanceState.getString(STATE_LAST_ENTITY_ID),
                savedInstanceState.getString(STATE_LAST_NOTIFICATION_ID)
            )
            val serializedPending = savedInstanceState.getStringArrayList(STATE_PENDING_NOTIFICATION_EVENTS)
            if (serializedPending != null) {
                notificationEventState.restorePending(
                    events = serializedPending.mapNotNull(::decodeNotificationEvent),
                    lastRecordedLink = lastLink,
                    eventId = savedInstanceState.getLong(STATE_NOTIFICATION_EVENT_ID)
                )
            } else {
                notificationEventState.restore(
                    eventId = savedInstanceState.getLong(STATE_NOTIFICATION_EVENT_ID),
                    link = NotificationDeepLink.fromPayload(type, entityId, notificationId),
                    lastRecordedLink = lastLink
                )
            }
            val incomingLink = NotificationDeepLink.fromIntent(intent)
            if (incomingLink != null && incomingLink != notificationEventState.lastRecordedLink) {
                notificationEventState.record(incomingLink)
            }
        } else {
            recordNotificationIntent(intent)
        }
        setContent {
            val notificationEvent = notificationEventState.current
            ControlTowerApp(
                notificationDeepLink = notificationEvent.link,
                notificationEventId = notificationEvent.id,
                onNotificationConsumed = notificationEventState::consume
            )
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        val event = notificationEventState.current
        outState.putBoolean(STATE_HAS_NOTIFICATION_EVENT, true)
        outState.putLong(STATE_NOTIFICATION_EVENT_ID, event.id)
        val link = event.link
        val type = when (link) {
            is NotificationDeepLink.ApprovalRequest -> "approval_request"
            is NotificationDeepLink.AgentTask -> "agent_task"
            is NotificationDeepLink.Unknown -> link.entityType
            null -> null
        }
        outState.putString(STATE_NOTIFICATION_ENTITY_TYPE, type)
        outState.putString(STATE_NOTIFICATION_ENTITY_ID, link?.entityId)
        outState.putString(STATE_NOTIFICATION_ID, link?.notificationId)
        val lastLink = notificationEventState.lastRecordedLink
        val lastType = when (lastLink) {
            is NotificationDeepLink.ApprovalRequest -> "approval_request"
            is NotificationDeepLink.AgentTask -> "agent_task"
            is NotificationDeepLink.Unknown -> lastLink.entityType
            null -> null
        }
        outState.putString(STATE_LAST_ENTITY_TYPE, lastType)
        outState.putString(STATE_LAST_ENTITY_ID, lastLink?.entityId)
        outState.putString(STATE_LAST_NOTIFICATION_ID, lastLink?.notificationId)
        outState.putStringArrayList(
            STATE_PENDING_NOTIFICATION_EVENTS,
            ArrayList(notificationEventState.pendingEvents().map(::encodeNotificationEvent))
        )
        super.onSaveInstanceState(outState)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        recordNotificationIntent(intent)
    }

    private fun encodeNotificationEvent(event: NotificationDeepLinkEvent): String {
        val link = event.link ?: return ""
        val type = when (link) {
            is NotificationDeepLink.ApprovalRequest -> "approval_request"
            is NotificationDeepLink.AgentTask -> "agent_task"
            is NotificationDeepLink.Unknown -> link.entityType
        }
        return listOf(event.id.toString(), type, link.entityId, link.notificationId.orEmpty()).joinToString("|")
    }

    private fun decodeNotificationEvent(value: String): NotificationDeepLinkEvent? {
        val parts = value.split('|')
        if (parts.size != 4) return null
        val id = parts[0].toLongOrNull()?.takeIf { it >= 0L } ?: return null
        val link = NotificationDeepLink.fromPayload(parts[1], parts[2], parts[3].ifEmpty { null }) ?: return null
        return NotificationDeepLinkEvent(id, link)
    }
}
