package com.aixion.controltower.core.ui.components

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.graphics.toPixelMap
import androidx.compose.ui.graphics.compositeOver
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.unit.dp
import androidx.compose.ui.platform.LocalDensity
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import kotlin.math.roundToInt

class ApprovalCardPreviewTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun inboxCardShowsBoundedSummaryAndMarksItIncomplete() {
        val longSummary = List(24) { index ->
            "step $index explains the requested action and its target"
        }.joinToString(" ")
        val approval = ApprovalSummary(
            id = "approval_long_summary",
            projectName = "Project",
            title = "Review long request",
            summary = longSummary,
            agentName = "Antigravity",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.MEDIUM,
            files = emptyList(),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = "Revert"
        )

        composeRule.setContent {
            ControlTowerTheme {
                Box(modifier = Modifier.width(280.dp)) {
                    ApprovalCard(approval = approval, onClick = {})
                }
            }
        }

        composeRule.onNodeWithTag("approval-card-summary-preview").assertExists()
        composeRule.onNodeWithTag("approval-card-preview-content").assertExists()
        composeRule.onNodeWithText("Request overview · preview only").assertExists()
        composeRule.onNodeWithText("Open full request before deciding").assertExists()
        composeRule.onNodeWithText("Review long request").assertExists()
        composeRule.onNodeWithText("Medium").assertExists()
    }

    @Test
    fun pendingApprovalStatusUsesCautionSurfaceAccent() {
        val approval = ApprovalSummary(
            id = "approval_pending_status_color",
            projectName = "Control Tower",
            title = "Review requested command",
            summary = "Run the requested command in this project.",
            agentName = "Antigravity",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.LOW,
            files = emptyList(),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = "Revert"
        )
        var expectedSurfaceColor = Color.Unspecified
        var sampleInsetPx = 0f

        composeRule.setContent {
            ControlTowerTheme {
                val palette = LocalTowerPalette.current
                expectedSurfaceColor = palette.caution.copy(alpha = 0.11f).compositeOver(palette.surfaceRaised)
                sampleInsetPx = with(LocalDensity.current) { 5.dp.toPx() }
                Box(modifier = Modifier.width(320.dp)) {
                    ApprovalCard(approval = approval)
                }
            }
        }

        composeRule.onNodeWithText("Requested").assertExists()
        val statusBounds = composeRule.onNodeWithTag("approval-card-status-badge")
            .fetchSemanticsNode().boundsInRoot
        val image = composeRule.onRoot().captureToImage().toPixelMap()
        val sampleX = (statusBounds.left + sampleInsetPx).roundToInt().coerceIn(0, image.width - 1)
        val sampleY = statusBounds.center.y.roundToInt().coerceIn(0, image.height - 1)

        assertEquals(
            "A waiting approval should use the caution-tinted status surface",
            expectedSurfaceColor.toArgb(),
            image[sampleX, sampleY].toArgb()
        )
    }

    @Test
    fun requestPreviewUsesTheExistingSoftSurfaceAccent() {
        val approval = ApprovalSummary(
            id = "approval_preview_surface",
            projectName = "Project",
            title = "Review request",
            summary = "Review the exact requested action.",
            agentName = "Antigravity",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.LOW,
            files = emptyList(),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = "Revert"
        )
        var expectedAccent = Color.Unspecified

        composeRule.setContent {
            ControlTowerTheme {
                expectedAccent = LocalTowerPalette.current.surfaceSubtle
                Box(modifier = Modifier.width(320.dp)) {
                    ApprovalCard(approval = approval)
                }
            }
        }

        val bounds = composeRule.onNodeWithTag("approval-card-summary-preview")
            .fetchSemanticsNode().boundsInRoot
        val image = composeRule.onRoot().captureToImage().toPixelMap()
        val sampleX = (bounds.left + 4f).roundToInt().coerceIn(0, image.width - 1)
        val sampleY = (bounds.top + bounds.height / 2f).roundToInt().coerceIn(0, image.height - 1)

        assertEquals(
            "Request preview should use the existing calm pastel surface token",
            expectedAccent.toArgb(),
            image[sampleX, sampleY].toArgb()
        )
    }

    @Test
    fun longExactToolPayloadIsNeverShownAsATruncatedInboxCommand() {
        val approval = ApprovalSummary(
            id = "approval_tool_preview",
            projectName = "Project",
            title = "Review command request",
            summary = "Run git status in the project.",
            agentName = "Antigravity",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.MEDIUM,
            files = listOf(
                FileChangeSummary(
                    path = "antigravity://tool/run_command",
                    changeType = "tool_call",
                    diff = "{\"command\":\"git status --short and then inspect every tracked file in the workspace\"}",
                    risk = RiskLevel.MEDIUM
                )
            ),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = "Revert"
        )

        composeRule.setContent {
            ControlTowerTheme {
                Box(modifier = Modifier.width(280.dp)) {
                    ApprovalCard(approval = approval, onClick = {})
                }
            }
        }

        composeRule.onNodeWithText("Exact tool action · preview only").assertExists()
        composeRule.onNodeWithText("Tool action is hidden in this preview.").assertExists()
        composeRule.onNodeWithText("Open full request before deciding").assertExists()
        composeRule.onAllNodesWithText("git status --short and then inspect every tracked file in the workspace")
            .assertCountEquals(0)
    }

    @Test
    fun cardHeightDoesNotDependOnSummaryLengthBeyondTheBoundedPreview() {
        val longSummary = List(24) { index ->
            "step $index explains the requested action and its target"
        }.joinToString(" ")
        val otherLongSummary = List(32) { index ->
            "action $index describes the requested change in detail"
        }.joinToString(" ")
        val firstHeight = renderCardHeight(longSummary)
        val secondHeight = renderCardHeight(otherLongSummary)

        assertTrue(
            "Long summaries should be capped by the bounded inbox preview",
            kotlin.math.abs(firstHeight - secondHeight) < 1f
        )
    }

    private fun renderCardHeight(summary: String): Float {
        val approval = ApprovalSummary(
            id = "approval_summary_height",
            projectName = "Project",
            title = "Review request",
            summary = summary,
            agentName = "Antigravity",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.MEDIUM,
            files = emptyList(),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = "Revert"
        )
        composeRule.setContent {
            ControlTowerTheme {
                Box(modifier = Modifier.width(280.dp)) {
                    ApprovalCard(approval = approval)
                }
            }
        }
        return composeRule.onNodeWithTag("approval-card").fetchSemanticsNode().boundsInRoot.height
    }

    @Test
    fun providerAndRiskArePlacedBeforeRequestPreviewAndExpiry() {
        val approval = ApprovalSummary(
            id = "approval_provider_priority",
            projectName = "Control Tower",
            title = "Review permission request",
            summary = "Run the requested command in this project.",
            agentName = "Antigravity",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.HIGH,
            files = emptyList(),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = "Revert",
            sourceProvider = "ANTIGRAVITY",
            sourceAgentName = "Antigravity on Mac",
            verifiedSource = true,
            expiresAt = "2026-10-23T12:00:00Z"
        )

        composeRule.setContent {
            ControlTowerTheme {
                Box(modifier = Modifier.width(280.dp)) {
                    ApprovalCard(approval = approval)
                }
            }
        }

        val provider = composeRule.onNodeWithText("Verified: Antigravity on Mac").fetchSemanticsNode()
        val risk = composeRule.onNodeWithText("High").fetchSemanticsNode()
        val title = composeRule.onNodeWithText("Review permission request").fetchSemanticsNode()
        val expiry = composeRule.onNodeWithText(formatApprovalExpiry(approval.expiresAt)).fetchSemanticsNode()

        assertTrue("Provider identity should appear before the request title", provider.boundsInRoot.top < title.boundsInRoot.top)
        assertTrue("Risk should appear before the request title", risk.boundsInRoot.top < title.boundsInRoot.top)
        assertTrue("Expiry should remain below the request title", expiry.boundsInRoot.top > title.boundsInRoot.top)
    }

    @Test
    fun tappableApprovalCardAnnouncesReviewActionToAssistiveTechnology() {
        val approval = ApprovalSummary(
            id = "approval_accessible_action",
            projectName = "Project",
            title = "Review permission request",
            summary = "Run the requested command in this project.",
            agentName = "Antigravity",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.MEDIUM,
            files = emptyList(),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = "Revert"
        )

        composeRule.setContent {
            ControlTowerTheme {
                ApprovalCard(approval = approval, onClick = {})
            }
        }

        val semantics = composeRule.onNodeWithTag("approval-card").fetchSemanticsNode().config
        assertTrue("Approval cards should retain their button role", semantics[SemanticsProperties.Role] == Role.Button)
        assertTrue(
            "Approval cards should tell TalkBack to open the full request before deciding",
            semantics[SemanticsActions.OnClick].label == "Open the full request before deciding"
        )
    }

    @Test
    fun cardLabelsToolInvocationsSeparatelyFromSingularAndPluralFileChanges() {
        val baseApproval = ApprovalSummary(
            id = "approval_payload_label",
            projectName = "Project",
            title = "Review requested work",
            summary = "Review the pending request.",
            agentName = "Antigravity",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.LOW,
            files = emptyList(),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = "Revert"
        )

        composeRule.setContent {
            ControlTowerTheme {
                ApprovalCard(
                    approval = baseApproval.copy(
                        files = listOf(
                            FileChangeSummary(
                                path = "antigravity://tool/run_command",
                                changeType = "tool_call",
                                diff = "{\"command\":\"git status\"}",
                                risk = RiskLevel.LOW
                            )
                        )
                    )
                )
            }
        }
        composeRule.onNodeWithText("Tool request").assertExists()

        composeRule.setContent {
            ControlTowerTheme {
                ApprovalCard(
                    approval = baseApproval.copy(
                        files = listOf(FileChangeSummary("README.md", "modify", "", RiskLevel.LOW))
                    )
                )
            }
        }
        composeRule.onNodeWithText("1 file").assertExists()

        composeRule.setContent {
            ControlTowerTheme {
                ApprovalCard(
                    approval = baseApproval.copy(
                        files = listOf(
                            FileChangeSummary("README.md", "modify", "", RiskLevel.LOW),
                            FileChangeSummary("src/Main.kt", "modify", "", RiskLevel.LOW)
                        )
                    )
                )
            }
        }
        composeRule.onNodeWithText("2 files").assertExists()
    }
}
