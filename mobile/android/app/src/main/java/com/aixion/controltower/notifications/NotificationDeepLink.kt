package com.aixion.controltower.notifications

import android.content.Intent

private const val EXTRA_NOTIFICATION_ID = "notification_id"
private const val EXTRA_ENTITY_TYPE = "entity_type"
private const val EXTRA_ENTITY_ID = "entity_id"
private val SAFE_ID = Regex("[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
private val SAFE_ENTITY_TYPE = Regex("[A-Za-z][A-Za-z0-9_.-]{0,63}")

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
            val cleanType = entityType?.takeIf(SAFE_ENTITY_TYPE::matches) ?: return null
            val cleanId = entityId?.takeIf(SAFE_ID::matches) ?: return null
            val cleanNotificationId = notificationId?.takeIf(SAFE_ID::matches)
            return when (cleanType) {
                "agent_task" -> AgentTask(cleanId, cleanNotificationId)
                "approval_request" -> ApprovalRequest(cleanId, cleanNotificationId)
                else -> Unknown(cleanType, cleanId, cleanNotificationId)
            }
        }
    }
}

internal fun NotificationDeepLink?.isApprovalRequest(): Boolean = this is NotificationDeepLink.ApprovalRequest

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
