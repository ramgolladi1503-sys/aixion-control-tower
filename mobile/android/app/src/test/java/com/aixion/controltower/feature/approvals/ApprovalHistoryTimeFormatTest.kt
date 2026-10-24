package com.aixion.controltower.feature.approvals

import java.util.Locale
import java.util.TimeZone
import org.junit.Assert.assertEquals
import org.junit.Test

class ApprovalHistoryTimeFormatTest {
    @Test
    fun decisionHistoryTimeIncludesTheLocalNumericUtcOffset() {
        val originalTimeZone = TimeZone.getDefault()
        val originalLocale = Locale.getDefault()
        try {
            TimeZone.setDefault(TimeZone.getTimeZone("Asia/Kolkata"))
            Locale.setDefault(Locale.US)

            assertEquals(
                "Oct 24, 2026 at 3:30 PM +05:30",
                formatHistoryTime("2026-10-24T10:00:00Z")
            )
        } finally {
            TimeZone.setDefault(originalTimeZone)
            Locale.setDefault(originalLocale)
        }
    }

    @Test
    fun invalidDecisionTimeRemainsExplicitlyUnavailable() {
        assertEquals("time unavailable", formatHistoryTime("not-an-instant"))
    }
}
