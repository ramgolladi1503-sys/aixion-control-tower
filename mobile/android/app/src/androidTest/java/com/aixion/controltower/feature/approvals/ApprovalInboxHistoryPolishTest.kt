package com.aixion.controltower.feature.approvals

import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import com.aixion.controltower.core.model.ApprovalHistoryEvent
import com.aixion.controltower.core.model.ApprovalHistoryRequestSnapshot
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import java.time.Instant

class ApprovalInboxHistoryPolishTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun staleApprovalFailureHasOnePoliteAnnouncementAndKeepsContextVisible() {
        composeRule.setContent {
            ControlTowerTheme {
                ApprovalInboxContent(
                    state = ApprovalsUiState(
                        loading = false,
                        approvals = listOf(staleApproval()),
                        lastSuccessfulSyncAt = Instant.parse("2026-10-22T05:30:00Z"),
                        errorMessage = "Backend unavailable"
                    )
                )
            }
        }

        val root = composeRule.onRoot(useUnmergedTree = true).fetchSemanticsNode()
        assertEquals(1, countPoliteLiveRegions(root))
        composeRule.onNodeWithTag("approval-inbox-stale-data-warning")
            .assertIsDisplayed()
            .assertTextContains("Previously loaded requests are hidden", substring = true)
        composeRule.onNodeWithTag("approval-inbox-last-successful-check")
            .assertIsDisplayed()
            .assertTextContains("Last successful check", substring = true)
        composeRule.onNodeWithTag("approval-inbox-sync-status")
            .assertIsDisplayed()
            .assertTextContains("Approval sync unavailable", substring = true)

        assertTrue(
            "The stale warning should be the only polite live region",
            composeRule.onNodeWithTag("approval-inbox-stale-data-warning")
                .fetchSemanticsNode().config[SemanticsProperties.LiveRegion] == LiveRegionMode.Polite
        )
        assertTrue(
            "The sync status should not announce again while stale warning is present",
            !composeRule.onNodeWithTag("approval-inbox-sync-status")
                .fetchSemanticsNode().config.contains(SemanticsProperties.LiveRegion)
        )
        assertTrue(
            "The timestamp should remain visible without a second announcement",
            !composeRule.onNodeWithTag("approval-inbox-last-successful-check")
                .fetchSemanticsNode().config.contains(SemanticsProperties.LiveRegion)
        )
    }

    @Test
    fun historyHeadingAndVerifiedRequestRemainAccessibleAndHierarchical() {
        composeRule.setContent {
            ControlTowerTheme {
                ApprovalInboxContent(
                    state = ApprovalsUiState(
                        loading = false,
                        approvalHistory = listOf(
                            ApprovalHistoryEvent(
                                eventId = "history-polish-event",
                                approvalId = "history-polish-approval",
                                decision = "approve",
                                reason = "Reviewed the exact request.",
                                decidedAt = "2026-10-22T10:00:00Z",
                                reviewerLabel = "Reviewer",
                                requestSnapshotVerified = true,
                                requestSnapshot = ApprovalHistoryRequestSnapshot(
                                    projectId = "project-id",
                                    title = "Verified request title",
                                    summary = "Request summary retained with the decision.",
                                    agentName = "Agent",
                                    targetBranch = "main",
                                    files = emptyList(),
                                    testPlan = emptyList(),
                                    rollbackPlan = null,
                                    sourceProvider = "ANTIGRAVITY",
                                    sourceAgentName = "Antigravity",
                                    sourceSessionId = null,
                                    sourceInvocationId = null,
                                    sourceTaskUrl = null,
                                    riskLevel = RiskLevel.LOW,
                                    riskReasons = emptyList(),
                                    requiredActions = emptyList()
                                )
                            )
                        ),
                        historyLoaded = true
                    )
                )
            }
        }

        composeRule.onNodeWithText("History").performClick()
        val heading = composeRule.onNodeWithTag("approval-inbox-heading")
            .assertIsDisplayed()
            .assertTextContains("Decision history")
        assertTrue(
            "Decision history should be exposed as a heading",
            heading.fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )

        val title = composeRule.onNodeWithTag("approval-history-request-title").assertIsDisplayed()
        val reviewer = composeRule.onNodeWithTag("approval-history-reviewer").assertIsDisplayed()
        assertTrue(
            "Request title should retain greater visual hierarchy than metadata",
            title.fetchSemanticsNode().boundsInRoot.height > reviewer.fetchSemanticsNode().boundsInRoot.height
        )
        composeRule.onNodeWithText("Request summary retained with the decision.").assertIsDisplayed()
    }

    private fun countPoliteLiveRegions(node: androidx.compose.ui.semantics.SemanticsNode): Int {
        val currentNodeMatches = node.config.contains(SemanticsProperties.LiveRegion) &&
            node.config[SemanticsProperties.LiveRegion] == LiveRegionMode.Polite
        return (if (currentNodeMatches) 1 else 0) + node.children.sumOf(::countPoliteLiveRegions)
    }

    private fun staleApproval() = ApprovalSummary(
        id = "stale-approval",
        projectName = "Project",
        title = "Stale request",
        summary = "This record must not be actionable after a failed refresh.",
        agentName = "Agent",
        targetBranch = "main",
        status = ApprovalStatus.REQUESTED,
        risk = RiskLevel.LOW,
        files = emptyList(),
        requiredActions = emptyList(),
        testPlan = emptyList(),
        rollbackPlan = ""
    )
}
