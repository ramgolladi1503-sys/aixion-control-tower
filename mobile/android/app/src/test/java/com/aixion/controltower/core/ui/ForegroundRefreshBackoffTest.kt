package com.aixion.controltower.core.ui

import org.junit.Assert.assertEquals
import org.junit.Test

class ForegroundRefreshBackoffTest {
    @Test
    fun failedRefreshDoublesDelayUpToFiveMinutes() {
        assertEquals(30_000L, nextForegroundRefreshDelayMillis(15_000, 15_000, false))
        assertEquals(300_000L, nextForegroundRefreshDelayMillis(240_000, 15_000, false))
        assertEquals(300_000L, nextForegroundRefreshDelayMillis(300_000, 15_000, false))
    }

    @Test
    fun successResetsDelayAndOverlappingRefreshDoesNotIncreaseIt() {
        assertEquals(15_000L, nextForegroundRefreshDelayMillis(120_000, 15_000, true))
        assertEquals(60_000L, nextForegroundRefreshDelayMillis(60_000, 15_000, null))
    }
}
