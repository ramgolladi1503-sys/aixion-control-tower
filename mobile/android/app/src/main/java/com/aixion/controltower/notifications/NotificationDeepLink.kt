package com.aixion.controltower.notifications

import android.content.Intent

private const val EXTRA_NOTIFICATION_ID = "notification_id"
private const val EXTRA_ENTITY_TYPE = "entity_type"
private const val EXTRA_ENTITY_ID = "entity_id"

sealed class NotificationDeepLink(
    open val entityId: String,
    open val notificationId: String?
) {
    data class AgentTask(
        override val entityId: String,
        override val notificationId: String? = null
    ) : NotificationDeepLink(entityId, notificationId)

    data class ApprovalRequest(
        override val entityId: String,
        override val notificationId: String? = null
    ) : NotificationDeepLink(entityId, notificationId)

    data class Unknown(
        val entityType: String,
        override val entityId: String,
        override val notificationId: String? = null
    ) : NotificationDeepLink(entityId, notificationId)

    companion object {
        fun fromIntent(intent: Intent?): NotificationDeepLink? {
            if (intent == null) return null
            val entityType = intent.getStringExtra(EXTRA_ENTITY_TYPE)?.takeIf { it.isNotBlank() } ?: return null
            val entityId = intent.getStringExtra(EXTRA_ENTITY_ID)?.takeIf { it.isNotBlank() } ?: return null
            val notificationId = intent.getStringExtra(EXTRA_NOTIFICATION_ID)?.takeIf { it.isNotBlank() }
            return fromPayload(entityType = entityType, entityId = entityId, notificationId = notificationId)
        }

        fun fromPayload(
            entityType: String?,
            entityId: String?,
            notificationId: String? = null
        ): NotificationDeepLink? {
            val cleanType = entityType?.takeIf { it.isNotBlank() } ?: return null
            val cleanId = entityId?.takeIf { it.isNotBlank() } ?: return null
            return when (cleanType) {
                "agent_task" -> AgentTask(cleanId, notificationId)
                "approval_request" -> ApprovalRequest(cleanId, notificationId)
                else -> Unknown(cleanType, cleanId, notificationId)
            }
        }
    }
}

fun Intent.putNotificationDeepLinkExtras(
    entityType: String,
    entityId: String,
    notificationId: String? = null
): Intent {
    putExtra(EXTRA_ENTITY_TYPE, entityType)
    putExtra(EXTRA_ENTITY_ID, entityId)
    if (!notificationId.isNullOrBlank()) {
        putExtra(EXTRA_NOTIFICATION_ID, notificationId)
    }
    return this
}
