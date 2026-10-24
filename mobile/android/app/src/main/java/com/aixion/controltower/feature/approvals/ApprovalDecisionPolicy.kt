package com.aixion.controltower.feature.approvals

import com.aixion.controltower.core.model.ApprovalSummary
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
