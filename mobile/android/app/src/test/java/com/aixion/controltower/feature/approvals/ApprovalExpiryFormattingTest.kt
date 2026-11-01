package com.aixion.controltower.feature.approvals

import java.time.ZoneId
import java.util.Locale
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ApprovalExpiryFormattingTest {
    @Test
    fun formatsValidExpiryAsLocalizedAbsoluteTimeInRequestedZone() {
        val formatted = formatLocalizedApprovalExpiry(
            value = "2027-01-02T03:04:05Z",
            locale = Locale.US,
            zoneId = ZoneId.of("America/Los_Angeles")
        )

        assertEquals("Jan 1, 2027, 7:04:05 PM", formatted)
    }

    @Test
    fun missingAndUnparseableExpiryRemainExplicit() {
        assertEquals("Not provided", formatLocalizedApprovalExpiry(null))
        assertEquals("not-a-timestamp", formatLocalizedApprovalExpiry("not-a-timestamp"))
    }

    @Test
    fun expiryFormattingShowsAnAbsoluteCalendarDate() {
        val formatted = formatLocalizedApprovalExpiry(
            value = "2027-01-02T03:04:05Z",
            locale = Locale.UK,
            zoneId = ZoneId.of("UTC")
        )

        assertTrue(formatted.contains("2027"))
        assertTrue(formatted.contains("Jan"))
    }
}
