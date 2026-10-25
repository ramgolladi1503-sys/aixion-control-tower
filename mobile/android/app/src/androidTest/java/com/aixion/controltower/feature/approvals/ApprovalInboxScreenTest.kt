package com.aixion.controltower.feature.approvals

import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithContentDescription
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.hasScrollAction
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performClick
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.graphics.toPixelMap
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.hasText
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalHistoryEvent
import com.aixion.controltower.core.model.ApprovalDeliveryStatus
import com.aixion.controltower.core.model.ApprovalHistoryFile
import com.aixion.controltower.core.model.ApprovalHistoryRequestSnapshot
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.ui.components.formatApprovalExpiry
import com.aixion.controltower.core.ui.ForegroundRefreshEffect
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import kotlin.math.roundToInt
import java.time.Instant

class ApprovalInboxScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun inboxUsesASectionSpecificAccessibleHeading() {
        composeRule.setContent {
            ControlTowerTheme {
                ApprovalInboxContent(state = ApprovalsUiState(loading = false))
            }
        }

        val heading = composeRule.onNodeWithTag("approval-inbox-heading")
        heading.assertTextContains("Needs your decision")
        assertTrue(
            "The inbox title should be exposed as a heading to assistive technology",
            heading.fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )

        composeRule.onNodeWithText("History").performClick()
        heading.assertTextContains("Decision history")
    }

    @Test
    fun selectingHistoryRefreshesEvenWhenHistoryWasAlreadyLoaded() {
        var historyLoadRequests = 0
        composeRule.setContent {
            ControlTowerTheme {
                ApprovalInboxContent(
                    state = ApprovalsUiState(
                        loading = false,
                        historyLoaded = true,
                        approvalHistory = listOf(
                            historyEvent(
                                id = "already-loaded-history",
                                decision = "approve",
                                reviewerLabel = "Reviewer",
                                requestSnapshotVerified = true,
                                requestSnapshot = historySnapshot("Existing decision")
                            )
                        )
                    ),
                    onHistorySelected = { historyLoadRequests++ }
                )
            }
        }

        composeRule.onNodeWithText("History").performClick()

        assertEquals(1, historyLoadRequests)
        composeRule.onNodeWithText("Existing decision").assertExists()
    }

    @Test
    fun manualRefreshWhileHistorySelectedRefreshesInboxAndHistory() {
        var inboxRefreshRequests = 0
        var historyRefreshRequests = 0
        composeRule.setContent {
            ControlTowerTheme {
                ApprovalInboxContent(
                    state = ApprovalsUiState(
                        loading = false,
                        historyLoaded = true,
                        approvalHistory = listOf(
                            historyEvent(
                                id = "history-refresh",
                                decision = "approve",
                                reviewerLabel = "Reviewer",
                                requestSnapshotVerified = true,
                                requestSnapshot = historySnapshot("Recorded decision")
                            )
                        )
                    ),
                    onRetry = { inboxRefreshRequests++ },
                    onHistorySelected = {},
                    onHistoryRetry = { historyRefreshRequests++ }
                )
            }
        }

        composeRule.onNodeWithText("History").performClick()
        composeRule.onNodeWithText("Refresh").performClick()

        assertEquals(1, inboxRefreshRequests)
        assertEquals(1, historyRefreshRequests)
    }

    @Test
    fun selectedHistorySectionAndFilterSurviveSavedStateRestoration() {
        val restorationTester = StateRestorationTester(composeRule)
        restorationTester.setContent {
            ControlTowerTheme {
                ApprovalInboxContent(
                    state = ApprovalsUiState(
                        loading = false,
                        historyLoaded = true,
                        approvalHistory = listOf(
                            historyEvent(
                                id = "approved-restoration",
                                decision = "approve",
                                reviewerLabel = "Reviewer",
                                requestSnapshotVerified = true,
                                requestSnapshot = historySnapshot("Approved record")
                            ),
                            historyEvent(
                                id = "denied-restoration",
                                decision = "deny",
                                reviewerLabel = "Reviewer",
                                requestSnapshotVerified = true,
                                requestSnapshot = historySnapshot("Denied record")
                            )
                        )
                    )
                )
            }
        }

        composeRule.onNodeWithText("History").performClick()
        composeRule.onNodeWithText("Denied").performClick()
        composeRule.onNodeWithText("Denied record").assertExists()
        composeRule.onAllNodesWithText("Approved record").assertCountEquals(0)

        restorationTester.emulateSavedInstanceStateRestore()

        composeRule.onNodeWithText("History").assertIsSelected()
        composeRule.onNodeWithText("Denied").assertIsSelected()
        composeRule.onNodeWithText("Denied record").assertExists()
        composeRule.onAllNodesWithText("Approved record").assertCountEquals(0)
    }

    @Test
    fun inboxSyncStatusIsAnnouncedWhenRefreshStateChanges() {
        val refreshing = androidx.compose.runtime.mutableStateOf(false)
        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(
                    loading = false,
                    refreshing = refreshing.value
                )
            )
        }

        val status = composeRule.onNodeWithTag("approval-inbox-sync-status")
        composeRule.onNodeWithText("Status not verified").assertExists()
        assertEquals(
            LiveRegionMode.Polite,
            status.fetchSemanticsNode().config[SemanticsProperties.LiveRegion]
        )

        composeRule.runOnIdle { refreshing.value = true }

        composeRule.onNodeWithText("Refreshing. Requests are read-only until sync completes.").assertExists()
        assertEquals(
            LiveRegionMode.Polite,
            status.fetchSemanticsNode().config[SemanticsProperties.LiveRegion]
        )
    }

    @Test
    fun inboxQueueContextRemainsReadableAtLargeFontScale() {
        composeRule.setContent {
            Box(modifier = Modifier.width(240.dp)) {
                CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                    ApprovalInboxContent(
                        state = ApprovalsUiState(
                            loading = false,
                            approvals = listOf(
                                approval("pending-heading", "Review this request", ApprovalStatus.REQUESTED)
                            )
                        )
                    )
                }
            }
        }

        val count = composeRule.onNodeWithTag("approval-inbox-pending-count")
        count.assertExists()
        val bounds = count.fetchSemanticsNode().boundsInRoot
        assertTrue("Pending count must remain within a narrow viewport", bounds.right <= 240f)
        assertTrue("Pending count must remain readable at large text", bounds.height > 24f)
        composeRule.onAllNodesWithText("1 request is waiting for your decision.").assertCountEquals(0)
    }

    @Test
    fun decisionHistoryLoadingIsAnnouncedPolitely() {
        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(loading = false, historyLoading = true)
            )
        }

        composeRule.onNodeWithText("History").performClick()
        val loadingStatus = composeRule.onNodeWithTag("approval-history-loading-status")
        loadingStatus.assertExists()
        loadingStatus.assertTextContains("Loading decision history", substring = true)
        assertEquals(
            LiveRegionMode.Polite,
            loadingStatus.fetchSemanticsNode().config[SemanticsProperties.LiveRegion]
        )
        composeRule.onNodeWithTag("approval-history-loading-skeletons").assertExists()
    }

    @Test
    fun approvalSkeletonsOnlyAppearDuringInitialLoadAndDisappearForEveryResult() {
        val state = androidx.compose.runtime.mutableStateOf(ApprovalsUiState(loading = true))
        composeRule.setContent { ApprovalInboxContent(state = state.value) }

        composeRule.onNodeWithTag("approval-inbox-loading-skeletons").assertExists()
        val loadingStatus = composeRule.onNodeWithTag("approval-inbox-sync-status")
        loadingStatus.assertTextContains("Loading approval requests", substring = true)
        assertEquals(
            LiveRegionMode.Polite,
            loadingStatus.fetchSemanticsNode().config[SemanticsProperties.LiveRegion]
        )

        composeRule.runOnIdle {
            state.value = ApprovalsUiState(
                loading = false,
                approvals = listOf(approval("loaded", "Loaded approval", ApprovalStatus.REQUESTED))
            )
        }
        composeRule.onAllNodesWithTag("approval-inbox-loading-skeletons").assertCountEquals(0)
        composeRule.onNodeWithText("Loaded approval").assertExists()

        composeRule.runOnIdle { state.value = ApprovalsUiState(loading = false) }
        composeRule.onAllNodesWithTag("approval-inbox-loading-skeletons").assertCountEquals(0)
        composeRule.onNodeWithText("No requests need review").assertExists()

        composeRule.runOnIdle {
            state.value = ApprovalsUiState(
                loading = false,
                errorMessage = "Backend unavailable"
            )
        }
        composeRule.onAllNodesWithTag("approval-inbox-loading-skeletons").assertCountEquals(0)
        val title = composeRule.onNodeWithText("Approval data unavailable").assertExists()
        assertTrue(title.fetchSemanticsNode().config.contains(SemanticsProperties.Heading))
    }

    @Test
    fun historySkeletonDisappearsAfterLoadedEmptyAndErrorResults() {
        val state = androidx.compose.runtime.mutableStateOf(
            ApprovalsUiState(loading = false, historyLoading = true)
        )
        composeRule.setContent { ApprovalInboxContent(state = state.value) }
        composeRule.onNodeWithText("History").performClick()
        composeRule.onNodeWithTag("approval-history-loading-skeletons").assertExists()

        composeRule.runOnIdle {
            state.value = ApprovalsUiState(
                loading = false,
                historyLoading = false,
                historyLoaded = true
            )
        }
        composeRule.onAllNodesWithTag("approval-history-loading-skeletons").assertCountEquals(0)
        composeRule.onNodeWithText("No decisions recorded").assertExists()

        composeRule.runOnIdle {
            state.value = ApprovalsUiState(
                loading = false,
                historyLoading = false,
                historyErrorMessage = "History unavailable"
            )
        }
        composeRule.onAllNodesWithTag("approval-history-loading-skeletons").assertCountEquals(0)
        composeRule.onNodeWithText("Decision history unavailable").assertExists()
    }

    @Test
    fun rendersApprovalBucketsForCanonicalAndLegacyStatuses() {
        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(
                    loading = false,
                    approvals = listOf(
                        approval(id = "approval_requested", title = "Pending approval", status = ApprovalStatus.REQUESTED),
                        approval(id = "approval_legacy", title = "Legacy pending", status = ApprovalStatus.PENDING_REVIEW),
                        approval(id = "approval_blocked", title = "Blocked approval", status = ApprovalStatus.BLOCKED),
                        approval(id = "approval_approved", title = "Approved approval", status = ApprovalStatus.APPROVED),
                    )
                )
            )
        }

        composeRule.onAllNodesWithText("Approvals").assertCountEquals(0)
        composeRule.onNodeWithText("2 pending").assertExists()
        composeRule.onAllNodesWithText("2 pending").assertCountEquals(1)
        composeRule.onAllNodesWithText("Needs review 2").assertCountEquals(0)
        composeRule.onNodeWithText("Status blocked 1").assertExists()
        val inboxList = composeRule.onNode(hasScrollAction())
        composeRule.onNodeWithText("Pending approval").assertExists()
        inboxList.performScrollToNode(hasText("Blocked approval"))
        composeRule.onNodeWithText("Blocked approval").assertExists()
        composeRule.onAllNodesWithText("Approved approval").assertCountEquals(0)
        inboxList.performScrollToNode(hasText("Pending approval"))
        composeRule.onAllNodesWithText("Aixion Control Tower").assertCountEquals(3)
        composeRule.onAllNodesWithContentDescription("Request summary: Regression approval summary")
            .assertCountEquals(3)
        composeRule.onAllNodesWithText("Low").assertCountEquals(3)
        composeRule.onNodeWithText("Pending review").assertExists()
        composeRule.onAllNodesWithText("Review request").assertCountEquals(3)
        composeRule.onAllNodesWithText("Review request").fetchSemanticsNodes().forEach { node ->
            assertEquals(Role.Button, node.config[SemanticsProperties.Role])
        }
    }

    @Test
    fun inboxSectionsWrapWithinNarrowLargeTextLayout() {
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 1.5f)) {
                Box(modifier = Modifier.width(240.dp).testTag("narrow-inbox-viewport")) {
                    ApprovalInboxContent(state = ApprovalsUiState(loading = false))
                }
            }
        }

        val reviewSection = composeRule.onNodeWithText("Needs review")
        val historySection = composeRule.onNodeWithText("History")
        reviewSection.assertExists()
        historySection.assertExists()
        val viewportBounds = composeRule.onNodeWithTag("narrow-inbox-viewport").fetchSemanticsNode().boundsInRoot
        val reviewBounds = reviewSection.fetchSemanticsNode().boundsInRoot
        val historyBounds = historySection.fetchSemanticsNode().boundsInRoot
        assertTrue(
            "Needs review tab should remain within the narrow viewport",
            reviewBounds.left >= viewportBounds.left && reviewBounds.right <= viewportBounds.right
        )
        assertTrue(
            "History tab should remain within the narrow viewport",
            historyBounds.left >= viewportBounds.left && historyBounds.right <= viewportBounds.right
        )
    }

    @Test
    fun historyFiltersWrapWithinNarrowLargeTextLayout() {
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 1.5f)) {
                Box(modifier = Modifier.width(240.dp).testTag("narrow-inbox-viewport")) {
                    ApprovalInboxContent(state = ApprovalsUiState(loading = false, historyLoaded = true))
                }
            }
        }

        composeRule.onNodeWithText("History").performClick()
        val filters = listOf("All", "Approved", "Denied", "Revision requested")
            .map(composeRule::onNodeWithText)
        filters.forEach { it.assertExists() }

        val viewportBounds = composeRule.onNodeWithTag("narrow-inbox-viewport").fetchSemanticsNode().boundsInRoot
        val filterBounds = filters.map { it.fetchSemanticsNode().boundsInRoot }
        filterBounds.forEachIndexed { index, bounds ->
            assertTrue("History filter ${index + 1} should be within the narrow viewport", bounds.left >= viewportBounds.left)
            assertTrue("History filter ${index + 1} should fit within the narrow viewport", bounds.right <= viewportBounds.right)
            assertTrue("History filter ${index + 1} should be visible vertically", bounds.top >= viewportBounds.top)
        }
        assertTrue(
            "History filters should wrap onto additional rows at narrow width",
            filterBounds.map { it.top }.distinct().size > 1
        )
    }

    @Test
    fun readOnlyInboxCardsDoNotShowReviewAffordance() {
        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(
                    loading = false,
                    refreshing = true,
                    approvals = listOf(
                        approval(id = "approval_refreshing", title = "Refreshing approval", status = ApprovalStatus.REQUESTED)
                    )
                )
            )
        }

        composeRule.onNodeWithText("Refreshing approval").assertExists()
        composeRule.onNodeWithText("Refreshing. Requests are read-only until sync completes.").assertExists()
        composeRule.onNodeWithText("1 pending").assertExists()
        composeRule.onAllNodesWithText("Review request").assertCountEquals(0)
    }

    @Test
    fun approvalCardsShowLocalExpiryOrExplicitUnavailableState() {
        val now = Instant.now()
        val validExpiry = now.plusSeconds(3_600).toString()
        val expiredExpiry = now.minusSeconds(3_600).toString()
        var selectedId: String? = null
        val approvals = listOf(
            approval("approval_valid_expiry", "Valid expiry", ApprovalStatus.REQUESTED)
                .copy(expiresAt = validExpiry),
            approval("approval_expired_expiry", "Expired request", ApprovalStatus.REQUESTED)
                .copy(expiresAt = expiredExpiry),
            approval("approval_missing_expiry", "Missing expiry", ApprovalStatus.REQUESTED),
            approval("approval_invalid_expiry", "Invalid expiry", ApprovalStatus.REQUESTED)
                .copy(expiresAt = "not-a-timestamp")
        )
        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(loading = false, approvals = approvals),
                onApprovalSelected = { selectedId = it.id }
            )
        }

        val validLabel = formatApprovalExpiry(validExpiry, now)
        val expiredLabel = formatApprovalExpiry(expiredExpiry, now)
        composeRule.onNodeWithText(validLabel).assertExists()
        composeRule.onNodeWithText(expiredLabel).assertExists()
        composeRule.onNodeWithContentDescription(
            "$expiredLabel. Request deadline passed; open details to check its current status."
        ).assertExists()
        composeRule.onAllNodesWithText("Expiry unavailable").assertCountEquals(2)

        composeRule.onNodeWithText("Expired request").performClick()
        assertEquals("Expired requests remain openable for status review", "approval_expired_expiry", selectedId)
    }

    @Test
    fun clickingApprovalCardReturnsSelectedApproval() {
        var selectedId: String? = null

        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(
                    loading = false,
                    approvals = listOf(
                        approval(id = "approval_a", title = "First approval", status = ApprovalStatus.REQUESTED),
                        approval(id = "approval_b", title = "Second approval", status = ApprovalStatus.REQUESTED),
                    )
                ),
                onApprovalSelected = { selectedId = it.id }
            )
        }

        composeRule.onNodeWithText("Second approval").performClick()

        assertEquals("approval_b", selectedId)
    }

    @Test
    fun rendersLoadingCopyWhileApprovalsLoad() {
        composeRule.setContent {
            ApprovalInboxContent(state = ApprovalsUiState(loading = true))
        }

        composeRule.onNodeWithText("Loading approval requests…").assertExists()
        composeRule.onNodeWithTag("approval-inbox-loading-skeletons").assertExists()
        composeRule.onAllNodesWithText("0 pending").assertCountEquals(0)
        composeRule.onAllNodesWithText("Needs review 0").assertCountEquals(0)
    }

    @Test
    fun initialApprovalLoadErrorShowsUnavailableStateWithoutStaleWarning() {
        var refreshRequested = false
        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(
                    loading = false,
                    errorMessage = "Backend unavailable: connection refused"
                ),
                onRetry = { refreshRequested = true }
            )
        }

        composeRule.onAllNodesWithText("Approval data unavailable").assertCountEquals(1)
        composeRule.onAllNodesWithText("Backend unavailable: connection refused").assertCountEquals(0)
        composeRule.onAllNodesWithText("Demo approvals are never shown.").assertCountEquals(0)
        composeRule.onNodeWithText(
            "Reconnect and retry to load the latest approvals. Requests stay unavailable until the server responds."
        ).assertExists()
        composeRule.onAllNodesWithText("Refresh").assertCountEquals(0)
        composeRule.onNodeWithText("Technical details").performClick()
        composeRule.onNodeWithText("Backend unavailable: connection refused").assertExists()
        composeRule.onNodeWithText("Hide technical details").assertExists()
        composeRule.onAllNodesWithText("Retry approvals").assertCountEquals(1)
        composeRule.onNodeWithText("Approval sync unavailable.").assertExists()
        composeRule.onAllNodesWithText("1 pending").assertCountEquals(0)
        composeRule.onAllNodesWithText("No fallback approvals loaded").assertCountEquals(0)
        composeRule.onAllNodesWithText("approval-inbox-stale-data-warning").assertCountEquals(0)
        composeRule.onAllNodesWithText("Previously loaded requests are hidden").assertCountEquals(0)

        composeRule.onNodeWithText("Retry approvals").performClick()
        assertEquals(true, refreshRequested)
    }

    @Test
    fun failedRefreshExplainsCachedApprovalsAreHiddenAndKeepsThemNonActionable() {
        val staleApproval = approval(
            id = "approval_stale_after_refresh_failure",
            title = "Previously loaded request",
            status = ApprovalStatus.REQUESTED
        )
        val syncedAt = Instant.parse("2026-10-22T05:30:00Z")

        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(
                    loading = false,
                    refreshing = false,
                    approvals = listOf(staleApproval),
                    lastSuccessfulSyncAt = syncedAt,
                    errorMessage = "Backend unavailable: connection refused"
                )
            )
        }

        val warning = composeRule.onNodeWithTag("approval-inbox-stale-data-warning")
        warning.assertExists()
        warning.assertTextContains("The latest approval check failed", substring = true)
        warning.assertTextContains("Previously loaded requests are hidden", substring = true)
        warning.assertTextContains("Reconnect and retry before reviewing", substring = true)
        composeRule.onNodeWithTag("approval-inbox-last-successful-check")
            .assertTextContains("Last successful check")
        assertEquals(
            LiveRegionMode.Polite,
            warning.fetchSemanticsNode().config[SemanticsProperties.LiveRegion]
        )
        composeRule.onAllNodesWithText("Previously loaded request").assertCountEquals(0)
        composeRule.onAllNodesWithText("1 pending").assertCountEquals(0)
        composeRule.onAllNodesWithText("Review request").assertCountEquals(0)
        composeRule.onNodeWithText("Retry approvals").assertExists()
    }

    @Test
    fun firstFetchFailureDoesNotInventLastSuccessfulCheckTime() {
        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(
                    loading = false,
                    errorMessage = "Backend unavailable: connection refused"
                )
            )
        }

        composeRule.onNodeWithText("Approval data unavailable").assertExists()
        composeRule.onAllNodesWithTag("approval-inbox-last-successful-check").assertCountEquals(0)
    }

    @Test
    fun emptyInboxCanRefreshAndInvokesReload() {
        var refreshRequested = false
        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(loading = false),
                onRetry = { refreshRequested = true }
            )
        }

        composeRule.onNodeWithText("Refresh").performClick()

        assertEquals(true, refreshRequested)
    }

    @Test
    fun caughtUpInboxDoesNotAssumeAnAgentIsAlreadyConnectedAndOffersConnectRoute() {
        var connectRequested = false
        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(loading = false),
                onOpenConnect = { connectRequested = true }
            )
        }

        composeRule.onAllNodesWithText("Status blocked 0").assertCountEquals(0)
        composeRule.onAllNodesWithText("0 pending").assertCountEquals(0)
        composeRule.onAllNodesWithText("Needs review 0").assertCountEquals(0)
        val caughtUp = composeRule.onNodeWithText("You’re all caught up").assertExists()
        assertTrue(caughtUp.fetchSemanticsNode().config.contains(SemanticsProperties.Heading))
        composeRule.onNodeWithText(
            "New approval requests will appear here when an agent needs your decision."
        ).assertExists()
        composeRule.onNodeWithText("Connect or manage agents").performClick()
        assertEquals(true, connectRequested)
    }

    @Test
    fun selectedInboxAndHistoryFiltersUseBrandFill() {
        var expectedBrand = Color.Unspecified
        composeRule.setContent {
            ControlTowerTheme {
                expectedBrand = LocalTowerPalette.current.brand
                ApprovalInboxContent(
                    state = ApprovalsUiState(loading = false, historyLoaded = true)
                )
            }
        }

        val needsReview = composeRule.onNodeWithText("Needs review")
        needsReview.assertIsSelected()
        assertSelectedFilterFillMatchesBrand("Needs review", expectedBrand)

        composeRule.onNodeWithText("History").performClick()
        composeRule.onNodeWithText("History").assertIsSelected()
        assertSelectedFilterFillMatchesBrand("History", expectedBrand)

        composeRule.onNodeWithText("All").assertIsSelected()
        assertSelectedFilterFillMatchesBrand("All", expectedBrand)
    }

    private fun assertSelectedFilterFillMatchesBrand(label: String, expectedBrand: Color) {
        val chip = composeRule.onNodeWithText(label)
        val bounds = chip.fetchSemanticsNode().boundsInRoot
        val image = composeRule.onRoot().captureToImage().toPixelMap()
        val sampleX = (bounds.left + bounds.width * 0.05f).roundToInt().coerceIn(0, image.width - 1)
        val sampleY = (bounds.top + bounds.height * 0.5f).roundToInt().coerceIn(0, image.height - 1)

        assertEquals(expectedBrand.toArgb(), image[sampleX, sampleY].toArgb())
    }

    @Test
    fun emptyInboxWithoutSuccessfulSyncDoesNotClaimItIsCaughtUp() {
        composeRule.setContent {
            ApprovalInboxContent(state = ApprovalsUiState(loading = false))
        }

        composeRule.onNodeWithText("Status not verified").assertExists()
        composeRule.onAllNodesWithText("You’re all caught up").assertCountEquals(0)
        composeRule.onNodeWithText("Refresh").assertExists()
    }

    @Test
    fun successfulEmptyInboxExplainsThatNewRequestsWillAppear() {
        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(
                    loading = false,
                    lastSuccessfulSyncAt = Instant.parse("2026-10-25T10:00:00Z")
                )
            )
        }

        composeRule.onNodeWithText("You’re all caught up").assertExists()
        composeRule.onNodeWithText(
            "New approval requests will appear here when an agent needs your decision."
        ).assertExists()
    }

    @Test
    fun unknownRiskRemainsVisibleWithDecisionsDisabled() {
        val unrecognized = approval(
            id = "approval_unknown_risk",
            title = "Unrecognized risk request",
            status = ApprovalStatus.REQUESTED
        ).copy(risk = RiskLevel.UNKNOWN)

        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(loading = false, approvals = listOf(unrecognized))
            )
        }

        composeRule.onNodeWithText("Unsupported data · decisions disabled").assertExists()
        composeRule.onNodeWithText("Unrecognized risk request").assertExists()
    }

    @Test
    fun foregroundRefreshRunsOnlyWhileScreenIsStarted() {
        var refreshCount = 0
        val lifecycleOwner = object : LifecycleOwner {
            override val lifecycle = LifecycleRegistry(this)
        }
        composeRule.setContent {
            CompositionLocalProvider(LocalLifecycleOwner provides lifecycleOwner) {
                ForegroundRefreshEffect(onRefresh = { refreshCount++; true }, intervalMillis = 25)
            }
        }

        composeRule.runOnIdle { lifecycleOwner.lifecycle.currentState = Lifecycle.State.CREATED }
        composeRule.runOnIdle { lifecycleOwner.lifecycle.currentState = Lifecycle.State.STARTED }
        composeRule.waitUntil(timeoutMillis = 2_000) { refreshCount >= 2 }
        composeRule.runOnIdle { lifecycleOwner.lifecycle.currentState = Lifecycle.State.CREATED }
        // Recompose with a stopped screen and ensure the cancelled loop stays quiet.
        composeRule.waitForIdle()
        var countAfterStop = 0
        composeRule.runOnIdle { countAfterStop = refreshCount }
        Thread.sleep(100)
        composeRule.runOnIdle { assertEquals(countAfterStop, refreshCount) }
    }

    @Test
    fun historyUsesImmutableDecisionAndVerifiedSnapshotInsteadOfCurrentApprovalStatus() {
        val currentApproval = approval(
            id = "approval-immutable-history",
            title = "Mutable record currently says approved",
            status = ApprovalStatus.APPROVED
        )
        val event = historyEvent(
            decision = "reject",
            reviewerLabel = "You",
            requestSnapshotVerified = true,
            requestSnapshot = historySnapshot(title = "Original request that was denied")
        )

        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(
                    loading = false,
                    approvals = listOf(currentApproval),
                    approvalHistory = listOf(event),
                    historyLoaded = true
                )
            )
        }

        composeRule.onNodeWithText("History").performClick()

        composeRule.onNodeWithText("All").assertIsSelected()
        composeRule.onNodeWithContentDescription("Decision status: Denied").assertExists()
        composeRule.onNodeWithText("Original request that was denied").assertExists()
        composeRule.onNodeWithText("Reviewer · You").assertExists()
        composeRule.onNodeWithText(
            "Immutable reviewer decisions for recent requests. Decision records do not confirm delivery to the agent or execution."
        ).assertExists()
        composeRule.onAllNodesWithText("Mutable record currently says approved").assertCountEquals(0)
        composeRule.onAllNodesWithText("Execution complete").assertCountEquals(0)
    }

    @Test
    fun historyHidesUnverifiedSnapshotAndShowsGenericUnavailableState() {
        val event = historyEvent(
            decision = "approve",
            reviewerLabel = "Workspace reviewer",
            requestSnapshotVerified = false,
            requestSnapshot = historySnapshot(title = "Unverified secret request")
        )

        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(
                    loading = false,
                    approvalHistory = listOf(event),
                    historyLoaded = true
                )
            )
        }

        composeRule.onNodeWithText("History").performClick()

        composeRule.onNodeWithText("All").assertIsSelected()
        composeRule.onNodeWithContentDescription("Decision status: Approved").assertExists()
        composeRule.onNodeWithText("Reviewer · Workspace reviewer").assertExists()
        composeRule.onNodeWithText("Request details unavailable").assertExists()
        composeRule.onNodeWithText("This older or unverifiable record is shown without request details.").assertExists()
        composeRule.onAllNodesWithText("Unverified secret request").assertCountEquals(0)
        composeRule.onAllNodesWithText("Delivered to agent").assertCountEquals(0)
    }

    @Test
    fun filteredHistoryEmptyStateNamesFilterAndOffersReset() {
        val approvedEvent = historyEvent(
            decision = "approve",
            reviewerLabel = "You",
            requestSnapshotVerified = false,
            requestSnapshot = null
        )
        composeRule.setContent {
            ControlTowerTheme {
                ApprovalInboxContent(
                    state = ApprovalsUiState(
                        loading = false,
                        approvalHistory = listOf(approvedEvent),
                        historyLoaded = true
                    )
                )
            }
        }

        composeRule.onNodeWithText("History").performClick()
        composeRule.onNodeWithText("Denied").performClick()

        composeRule.onNodeWithText("No denied decisions match").assertExists()
        composeRule.onNodeWithText("Show all decisions").performClick()
        composeRule.onAllNodesWithText("Approved").assertCountEquals(2)
        composeRule.onAllNodesWithText("No decisions recorded").assertCountEquals(0)
    }

    @Test
    fun historyShowsAccessibleLoadOlderActionAndRetryState() {
        var loadRequests = 0
        composeRule.setContent {
            ControlTowerTheme {
                ApprovalInboxContent(
                    state = ApprovalsUiState(
                        loading = false,
                        historyLoaded = true,
                        historyHasMore = true,
                        historyNextCursor = "opaque-cursor",
                        historyLoadMoreError = "Older decisions could not be loaded. Retry when the backend is reachable."
                    ),
                    onLoadOlderHistory = { loadRequests += 1 }
                )
            }
        }

        composeRule.onNodeWithText("History").performClick()
        composeRule.onNodeWithText(
            "Older decisions could not be loaded. Retry when the backend is reachable."
        ).assertExists()
        composeRule.onNodeWithText("Retry loading older decisions").performClick()

        assertEquals(1, loadRequests)
    }

    @Test
    fun historyShowsDeliveryStatusSeparatelyFromDecisionAndUsesConservativeCopy() {
        val events = listOf(
            historyEvent(
                id = "audit-not-confirmed",
                decision = "approve",
                deliveryStatus = ApprovalDeliveryStatus.NOT_CONFIRMED,
                reviewerLabel = "You",
                requestSnapshotVerified = false,
                requestSnapshot = null
            ),
            historyEvent(
                id = "audit-consumed",
                decision = "approve",
                deliveryStatus = ApprovalDeliveryStatus.CONSUMED,
                reviewerLabel = "You",
                requestSnapshotVerified = false,
                requestSnapshot = null
            ),
            historyEvent(
                id = "audit-denied",
                decision = "reject",
                deliveryStatus = ApprovalDeliveryStatus.NOT_APPLICABLE,
                reviewerLabel = "You",
                requestSnapshotVerified = false,
                requestSnapshot = null
            ),
            historyEvent(
                id = "audit-failure",
                decision = "approve",
                deliveryStatus = ApprovalDeliveryStatus.DELIVERY_UNCONFIRMED,
                reviewerLabel = "You",
                requestSnapshotVerified = false,
                requestSnapshot = null
            )
        )
        composeRule.setContent {
            ControlTowerTheme {
                ApprovalInboxContent(
                    state = ApprovalsUiState(
                        loading = false,
                        approvalHistory = events,
                        historyLoaded = true
                    )
                )
            }
        }

        composeRule.onNodeWithText("History").performClick()
        composeRule.onNodeWithText("All").assertIsSelected()
        val historyList = composeRule.onNode(hasScrollAction())
        historyList.performScrollToNode(hasText("The server has no usable delivery evidence for this decision."))
        composeRule.onAllNodesWithContentDescription("Decision status: Approved").assertCountEquals(2)
        composeRule.onNodeWithText("Delivery not confirmed").assertExists()
        composeRule.onNodeWithText(
            "The server has no usable delivery evidence for this decision."
        ).assertExists()
        historyList.performScrollToNode(hasText("Decision consumed"))
        composeRule.onAllNodesWithContentDescription("Decision status: Approved").assertCountEquals(2)
        composeRule.onNodeWithText("Decision consumed").assertExists()
        historyList.performScrollToNode(hasText("No delivery expected"))
        composeRule.onAllNodesWithContentDescription("Decision status: Denied").assertCountEquals(1)
        composeRule.onNodeWithText("No delivery expected").assertExists()
        composeRule.onNodeWithText(
            "This decision does not authorize an external agent to resume."
        ).assertExists()
        historyList.performScrollToNode(hasText("Delivery response ambiguous"))
        composeRule.onAllNodesWithContentDescription("Decision status: Approved").assertCountEquals(2)
        composeRule.onNodeWithText("Delivery response ambiguous").assertExists()
        composeRule.onNodeWithText(
            "The connector claimed this decision once. This confirms protocol consumption, not agent execution or task completion."
        ).assertExists()
        composeRule.onNodeWithText(
            "The connector reported an ambiguous claim response. The server may have recorded the claim; connector receipt and agent execution or resumption remain unconfirmed."
        ).assertExists()
    }
}

private fun historyEvent(
    id: String = "audit-event-test-only",
    decision: String,
    deliveryStatus: ApprovalDeliveryStatus = ApprovalDeliveryStatus.NOT_CONFIRMED,
    reviewerLabel: String,
    requestSnapshotVerified: Boolean,
    requestSnapshot: ApprovalHistoryRequestSnapshot?
) = ApprovalHistoryEvent(
    eventId = id,
    approvalId = "approval-$id",
    decision = decision,
    reason = "Checked the requested changes.",
    decidedAt = "2026-10-22T10:00:00Z",
    reviewerLabel = reviewerLabel,
    deliveryStatus = deliveryStatus,
    requestSnapshotVerified = requestSnapshotVerified,
    requestSnapshot = requestSnapshot
)

private fun historySnapshot(title: String) = ApprovalHistoryRequestSnapshot(
    projectId = "private-project-id",
    title = title,
    summary = "A request summary preserved with the decision.",
    agentName = "Antigravity",
    targetBranch = "main",
    files = listOf(ApprovalHistoryFile("src/Main.kt", "modify")),
    testPlan = listOf("Run focused tests"),
    rollbackPlan = "Revert the change",
    sourceProvider = "ANTIGRAVITY",
    sourceAgentName = "Antigravity on Mac",
    sourceSessionId = "private-session-id",
    sourceInvocationId = "private-invocation-id",
    sourceTaskUrl = "https://example.invalid/task",
    riskLevel = RiskLevel.HIGH,
    riskReasons = listOf("Review carefully"),
    requiredActions = listOf("Review")
)

private fun approval(id: String, title: String, status: ApprovalStatus): ApprovalSummary {
    return ApprovalSummary(
        id = id,
        projectName = "Aixion Control Tower",
        title = title,
        summary = "Regression approval summary",
        agentName = "codex-agent",
        targetBranch = "feature/regression",
        status = status,
        risk = RiskLevel.LOW,
        files = listOf(
            FileChangeSummary(
                path = "docs/regression.md",
                changeType = "create",
                diff = "+ regression",
                risk = RiskLevel.LOW
            )
        ),
        requiredActions = listOf("Review files"),
        testPlan = listOf("python -m pytest"),
        rollbackPlan = "Close PR"
    )
}
