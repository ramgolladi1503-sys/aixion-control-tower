package com.aixion.controltower.feature.approvals

import com.aixion.controltower.core.model.ApprovalSummary
import java.time.Duration
import java.time.Instant
import java.time.OffsetDateTime
import java.time.format.DateTimeParseException

/** Fail-closed client checks before asking the server to commit a reviewer decision. */
internal object ApprovalDecisionPolicy {
    fun expiryInstant(value: String?): Instant? {
        if (value.isNullOrBlank()) return null
        return try {
            OffsetDateTime.parse(value).toInstant()
        } catch (_: DateTimeParseException) {
            null
        }
    }

    fun expiryCountdownLabel(value: String?, now: Instant): String {
        val expiry = expiryInstant(value) ?: return "Expiry unavailable"
        val secondsLeft = Duration.between(now, expiry).seconds
        if (secondsLeft <= 0) return "Expired"
        if (secondsLeft < 60) return "Expires in less than a minute"

        val minutesLeft = (secondsLeft + 59) / 60
        if (minutesLeft < 60) {
            val unit = if (minutesLeft == 1L) "minute" else "minutes"
            return "Expires in about $minutesLeft $unit"
        }

        val hoursLeft = (minutesLeft + 59) / 60
        val unit = if (hoursLeft == 1L) "hour" else "hours"
        return "Expires in about $hoursLeft $unit"
    }

    fun expiryIsUrgent(value: String?, now: Instant): Boolean =
        expiryInstant(value)?.let { !it.isAfter(now.plusSeconds(5 * 60)) } ?: false

    fun blockReason(approval: ApprovalSummary, now: Instant): String? {
        val expiresAt = expiryInstant(approval.expiresAt)
            ?: return "This request has no valid expiry. Refresh it before deciding."
        if (!expiresAt.isAfter(now)) {
            return "This request has expired. Refresh the inbox before deciding."
        }

        if (!approval.sourceProvider.equals("MANUAL", ignoreCase = true)) {
            if (!approval.verifiedSource) {
                return "The connector source is not verified. Do not decide on this request."
            }
            if (approval.sourceAgentId.isNullOrBlank() ||
                approval.sourceSessionId.isNullOrBlank() ||
                approval.sourceInvocationId.isNullOrBlank()
            ) {
                return "The connector installation, session, or invocation identity is missing."
            }
        }
        return null
    }
}
