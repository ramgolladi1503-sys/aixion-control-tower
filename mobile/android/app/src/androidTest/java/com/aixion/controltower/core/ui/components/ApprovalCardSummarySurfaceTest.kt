package com.aixion.controltower.core.ui.components

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.graphics.toPixelMap
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import kotlin.math.roundToInt

class ApprovalCardSummarySurfaceTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun requestSummaryUsesExistingSubtleSurfaceToken() {
        lateinit var expectedSurface: Color
        composeRule.setContent {
            ControlTowerTheme {
                expectedSurface = LocalTowerPalette.current.surfaceSubtle
                ApprovalCard(
                    approval = ApprovalSummary(
                        id = "approval-summary-surface",
                        projectName = "Payments",
                        title = "Run the requested check",
                        summary = "The agent requested this exact action for review.",
                        agentName = "Antigravity",
                        targetBranch = "main",
                        status = ApprovalStatus.REQUESTED,
                        risk = RiskLevel.MEDIUM,
                        files = emptyList(),
                        requiredActions = emptyList(),
                        testPlan = emptyList(),
                        rollbackPlan = ""
                    )
                )
            }
        }

        composeRule.onNodeWithText("The agent requested this exact action for review.").assertExists()
        val previewBounds = composeRule.onNodeWithTag("approval-card-summary-surface")
            .fetchSemanticsNode().boundsInRoot
        val summaryPixel = composeRule.onRoot().captureToImage().toPixelMap()[
            (previewBounds.left + 5f).roundToInt(),
            previewBounds.center.y.roundToInt()
        ]

        assertEquals("Request summary should use the calm surface accent", expectedSurface.toArgb(), summaryPixel.toArgb())
    }
}
