package com.aixion.controltower.core.ui.theme

import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class TowerDesignTokensTest {
    @Test
    fun primaryTypographyMatchesTheProductionReadingScale() {
        assertEquals(28.sp, TowerTypography.displayLarge.fontSize)
        assertEquals(34.sp, TowerTypography.displayLarge.lineHeight)
        assertEquals(28.sp, TowerTypography.headlineLarge.fontSize)
        assertEquals(34.sp, TowerTypography.headlineLarge.lineHeight)
        assertEquals(20.sp, TowerTypography.titleLarge.fontSize)
        assertEquals(26.sp, TowerTypography.titleLarge.lineHeight)
        assertEquals(16.sp, TowerTypography.bodyMedium.fontSize)
        assertEquals(24.sp, TowerTypography.bodyMedium.lineHeight)
    }

    @Test
    fun bodyLargeMatchesApprovalReadingContract() {
        val body = TowerTypography.bodyLarge

        assertEquals(16.sp, body.fontSize)
        assertEquals(24.sp, body.lineHeight)
    }

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
