package com.aixion.controltower.core.ui.theme

import androidx.compose.ui.unit.dp
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class TowerDesignTokensTest {
    @Test
    fun releaseOneCardAndSpacingTokensFollowTheDesignContract() {
        assertEquals(16.dp, TowerRadius.md)
        assertEquals(16.dp, TowerSpacing.lg)

        listOf(
            TowerSpacing.xs,
            TowerSpacing.sm,
            TowerSpacing.md,
            TowerSpacing.lg,
            TowerSpacing.xl,
            TowerSpacing.xxl
        ).forEach { spacing ->
            assertTrue("$spacing must stay on the 4dp grid", spacing.value % 4f == 0f)
        }
    }
}
