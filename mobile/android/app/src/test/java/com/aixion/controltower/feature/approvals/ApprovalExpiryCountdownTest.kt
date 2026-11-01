package com.aixion.controltower.feature.approvals

import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Test

class ApprovalExpiryCountdownTest {
    private val now = Instant.parse("2026-10-25T12:00:00Z")

    @Test
    fun countdownUsesReadableMinuteAndHourUnits() {
        assertEquals(
            "Expires in about 2 minutes",
            formatApprovalExpiryCountdown("2026-10-25T12:01:01Z", now)
        )
        assertEquals(
            "Expires in about 2 hours",
            formatApprovalExpiryCountdown("2026-10-25T13:01:00Z", now)
        )
    }

    @Test
    fun countdownCallsOutImmediateAndPastExpiry() {
        assertEquals(
            "Expires in less than a minute",
            formatApprovalExpiryCountdown("2026-10-25T12:00:59Z", now)
        )
        assertEquals(
            "Expired",
            formatApprovalExpiryCountdown("2026-10-25T11:59:59Z", now)
        )
    }

    @Test
    fun countdownDoesNotHideMissingOrInvalidExpiry() {
        assertEquals("Expiry unavailable", formatApprovalExpiryCountdown(null, now))
        assertEquals("Expiry unavailable", formatApprovalExpiryCountdown("not-a-time", now))
    }
}
