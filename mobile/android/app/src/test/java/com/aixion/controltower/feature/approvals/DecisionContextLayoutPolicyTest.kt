package com.aixion.controltower.feature.approvals

import androidx.compose.ui.unit.dp
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DecisionContextLayoutPolicyTest {
    @Test
    fun stacksMetadataForNarrowWindowsAndLargeFontScales() {
        assertTrue(shouldStackDecisionContext(maxWidth = 299.dp, fontScale = 1f))
        assertTrue(shouldStackDecisionContext(maxWidth = 390.dp, fontScale = 2f))
    }

    @Test
    fun keepsTwoColumnMetadataOnWideWindowsAtNormalFontScale() {
        assertFalse(shouldStackDecisionContext(maxWidth = 300.dp, fontScale = 1f))
        assertFalse(shouldStackDecisionContext(maxWidth = 359.dp, fontScale = 1f))
        assertFalse(shouldStackDecisionContext(maxWidth = 480.dp, fontScale = 1.3f))
    }

    @Test
    fun usesSideBySideActionsOnlyWhenShortViewportHasRoom() {
        assertTrue(shouldUseHorizontalCompactDecisionActions(maxWidth = 800.dp, fontScale = 1f))
        assertFalse(shouldUseHorizontalCompactDecisionActions(maxWidth = 599.dp, fontScale = 1f))
        assertFalse(shouldUseHorizontalCompactDecisionActions(maxWidth = 800.dp, fontScale = 1.5f))
    }
}
