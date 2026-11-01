package com.aixion.controltower.core.ui.components

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.graphics.toPixelMap
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import kotlin.math.roundToInt

class ApprovalCardIdentityRuleTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun approvalCardShowsOliveIdentityRuleInsideItsSurface() {
        val approval = ApprovalSummary(
            id = "approval_identity_rule",
            projectName = "Project",
            title = "Review workspace request",
            summary = "Review the requested workspace changes before deciding.",
            agentName = "Antigravity",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.MEDIUM,
            files = emptyList(),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = "Revert"
        )
        var expectedBrand = Color.Unspecified
        var sampleInsetPx = 0f

        composeRule.setContent {
            ControlTowerTheme {
                expectedBrand = LocalTowerPalette.current.brand
                sampleInsetPx = with(LocalDensity.current) { 5.5.dp.toPx() }
                Box(modifier = Modifier.width(320.dp)) {
                    ApprovalCard(approval = approval, onClick = {})
                }
            }
        }

        val cardBounds = composeRule.onNodeWithTag("approval-card")
            .fetchSemanticsNode().boundsInRoot
        val image = composeRule.onRoot().captureToImage().toPixelMap()
        val sampleX = (cardBounds.left + sampleInsetPx).roundToInt().coerceIn(0, image.width - 1)
        val sampleY = cardBounds.center.y.roundToInt().coerceIn(0, image.height - 1)

        assertTrue("The card must have room for an inset accent", cardBounds.height > 32f)
        assertEquals(
            "The inset identity rule should use the calm theme brand color",
            expectedBrand.toArgb(),
            image[sampleX, sampleY].toArgb()
        )
    }
}
