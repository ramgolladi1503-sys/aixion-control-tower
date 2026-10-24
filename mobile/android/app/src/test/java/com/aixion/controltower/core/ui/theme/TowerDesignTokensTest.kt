package com.aixion.controltower.core.ui.theme

import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class TowerDesignTokensTest {
    @Test
    fun pageHeadingMatchesSharedLargeHeadingContract() {
        val heading = TowerTypography.headlineLarge

        assertEquals(28.sp, heading.fontSize)
        assertEquals(36.sp, heading.lineHeight)
        assertEquals(androidx.compose.ui.text.font.FontWeight.SemiBold, heading.fontWeight)
    }

    @Test
    fun bodyLargeMatchesApprovalReadingContract() {
        val body = TowerTypography.bodyLarge

        assertEquals(16.sp, body.fontSize)
        assertEquals(24.sp, body.lineHeight)
    }

    @Test
    fun supportingTypographyMatchesItsRoleAndKeepsLabelsReadable() {
        val bodySmall = TowerTypography.bodySmall
        val labelMedium = TowerTypography.labelMedium
        val labelSmall = TowerTypography.labelSmall

        assertEquals(14.sp, bodySmall.fontSize)
        assertEquals(20.sp, bodySmall.lineHeight)
        assertEquals(14.sp, labelMedium.fontSize)
        assertEquals(20.sp, labelMedium.lineHeight)
        assertEquals(14.sp, labelSmall.fontSize)
        assertEquals(18.sp, labelSmall.lineHeight)
        assertTrue("Labels must remain at least 14sp", labelMedium.fontSize >= 14.sp)
        assertTrue("Labels must remain at least 14sp", labelSmall.fontSize >= 14.sp)
        assertTrue("Supporting styles must retain distinct hierarchy", bodySmall != labelMedium)
        assertTrue("Supporting styles must retain distinct hierarchy", labelMedium != labelSmall)
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

    @Test
    fun contentFrameUsesCompactAndExpandedMaximumWidths() {
        assertEquals(TowerLayout.compactContentMaxWidth, towerContentMaxWidth(600.dp))
        assertEquals(TowerLayout.compactContentMaxWidth, towerContentMaxWidth(839.dp))
        assertEquals(TowerLayout.expandedContentMaxWidth, towerContentMaxWidth(840.dp))
        assertEquals(TowerLayout.expandedContentMaxWidth, towerContentMaxWidth(1_200.dp))
    }
}
