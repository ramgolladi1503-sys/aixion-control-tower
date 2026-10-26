package com.aixion.controltower.feature.connectors

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class ConnectorsScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun showsSimpleNativeProviderSetupAndDoesNotExposeWebhookTaskIntake() {
        composeRule.setContent { ConnectorsContent() }

        composeRule.onNodeWithText("Set up a project").assertExists()
        composeRule.onNodeWithText("Connect an agent").assertExists()
        composeRule.onNodeWithText("How connection works").performClick()
        composeRule.onNodeWithText("Before you connect").assertExists()
        composeRule.onNodeWithText("Open the project").assertExists()
        composeRule.onNodeWithText(
            "Use the computer and agent session that should pause for approvals."
        ).assertExists()
        composeRule.onNodeWithText("Keep the computer available").assertExists()
        composeRule.onNodeWithText(
            "Keep it powered on and awake, with the agent session and Control Tower API reachable while a request is waiting."
        ).assertExists()
        composeRule.onNodeWithText("Save your one-time token").assertExists()
        composeRule.onNodeWithText(
            "Setup shows the connector token once. Store it like a password."
        ).assertExists()
        composeRule.onNodeWithText(
            "The phone cannot wake or restart the agent computer. This screen does not verify that the provider hook is installed."
        ).assertExists()
        composeRule.onAllNodesWithText("CONNECT ONCE").assertCountEquals(0)
        composeRule.onNodeWithText("Choose a provider").assertExists()
        composeRule.onNodeWithText(
            "Choose the provider used by this project. Complete setup on the computer where the agent runs."
        ).assertExists()
        composeRule.onNodeWithText("Antigravity").assertExists()
        composeRule.onNodeWithText("Codex").performScrollTo().assertExists()
        composeRule.onNodeWithText("Open Antigravity setup").assertExists()
        composeRule.onNodeWithText(
            "Antigravity's local pre-tool hook is the supported approval path. Install it on the computer running this project, and keep that computer, session, and API reachable while a request waits."
        ).performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("View setup only (unverified)").performScrollTo().assertExists()
        composeRule.onNodeWithText(
            "Codex Desktop phone approval delivery and returning a decision to the same task are not verified. This setup page does not establish a live approval connection. Do not rely on it for live work."
        ).performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Unverified · not a live connection").assertExists()
        composeRule.onAllNodesWithText("Setup available").assertCountEquals(0)
        composeRule.onNodeWithText("When an approval arrives").performScrollTo().assertExists()

        composeRule.onAllNodesWithText("Other integrations and webhooks").assertCountEquals(0)
        composeRule.onAllNodesWithText("Webhook task intake").assertCountEquals(0)
        composeRule.onAllNodesWithText("Choose a webhook integration").assertCountEquals(0)
        composeRule.onAllNodesWithText("Create connection").assertCountEquals(0)
    }

    @Test
    fun approvalFlowExplainsReviewAndSameSessionRequirement() {
        val explanation =
            "The request appears in Approvals. Review the exact action, then approve or deny it. " +
            "The decision returns to the same waiting task only while its computer, session, and API remain reachable."
        composeRule.setContent { ConnectorsContent() }

        composeRule.onNodeWithText("How connection works").performScrollTo().performClick()
        composeRule.onNodeWithText("When an approval arrives")
            .performScrollTo()
            .assertIsDisplayed()
        val approvalFlowHeading = composeRule.onNodeWithText("When an approval arrives")
        assertTrue(
            "The approval flow explanation should be available through screen-reader heading navigation",
            approvalFlowHeading.fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
        composeRule.onNodeWithText(explanation, useUnmergedTree = true)
            .assertExists()
            .assertTextEquals(explanation)
            .assertIsDisplayed()
    }

    @Test
    fun connectionGuidanceStartsCollapsedAndExpandsWithoutHidingProviderChoices() {
        composeRule.setContent { ConnectorsContent() }

        composeRule.onNodeWithText("Open Antigravity setup").assertExists()
        composeRule.onNodeWithText("How connection works")
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.StateDescription, "Collapsed"))
        composeRule.onNodeWithText("Before you connect").assertDoesNotExist()

        composeRule.onNodeWithText("How connection works").performClick()
        composeRule.onNodeWithText("Hide connection details")
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.StateDescription, "Expanded"))
        composeRule.onNodeWithText("Before you connect").assertExists()
        composeRule.onNodeWithText("Keep the computer available").assertExists()
        composeRule.onNodeWithText("When an approval arrives").assertExists()
        composeRule.onNodeWithText("Hide connection details").performClick()
        composeRule.onNodeWithText("Before you connect").assertDoesNotExist()
        composeRule.onNodeWithText("Open Antigravity setup").assertExists()
    }

    @Test
    fun providerSetupActionsOpenTheMatchingPlatformSetup() {
        val navigationRequests = mutableListOf<String>()
        composeRule.setContent {
            ConnectorsContent(onOpenAgentAccess = { navigationRequests += it })
        }

        composeRule.onNodeWithText("Open Antigravity setup").performScrollTo().performClick()
        composeRule.onNodeWithText("View setup only (unverified)").performScrollTo().performClick()

        composeRule.runOnIdle {
            assertEquals(listOf("ANTIGRAVITY", "CODEX"), navigationRequests)
        }
    }

    @Test
    fun providerReadinessStatesAreDistinctAndDoNotClaimLiveConnection() {
        composeRule.setContent { ConnectorsContent() }

        composeRule.onNodeWithText("Supported hook · setup required").assertExists()
        composeRule.onAllNodesWithText("Supported hook · setup required").assertCountEquals(1)
        composeRule.onNodeWithText("Earlier build tested").assertDoesNotExist()
        composeRule.onNodeWithText(
            "Antigravity's local pre-tool hook is the supported approval path. Install it on the computer running this project, and keep that computer, session, and API reachable while a request waits."
        ).performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Phone test passed locally").assertDoesNotExist()
        composeRule.onNodeWithText("Unverified · not a live connection").assertExists()
        composeRule.onNodeWithText("View setup only (unverified)").assertExists()
        composeRule.onAllNodesWithText("Antigravity connected").assertCountEquals(0)
        composeRule.onAllNodesWithText("Codex connected").assertCountEquals(0)
        composeRule.onAllNodesWithText("Connected").assertCountEquals(0)
        composeRule.onAllNodesWithText("FIRST SUPPORTED").assertCountEquals(0)
    }

    @Test
    fun setupGuidanceWrapsAtLargeFontScaleOnNarrowScreen() {
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                Box(modifier = Modifier.width(240.dp)) {
                    ConnectorsContent()
                }
            }
        }

        val heading = composeRule.onNodeWithText("Connect an agent")
        heading.assertExists()
        composeRule.onNodeWithTag("connect-hero-logo").assertDoesNotExist()
        assertTrue(
            "Connect page title should be exposed as a screen-reader heading",
            heading.fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
        val bounds = heading.fetchSemanticsNode().boundsInRoot
        assertTrue("Connect heading must remain within the narrow viewport", bounds.right <= 240f)
        assertTrue("Connect heading must wrap at large font scale", bounds.height > 48f)

        composeRule.onNodeWithText("Choose a provider").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Supported hook · setup required").performScrollTo().assertIsDisplayed()
    }

    @Test
    fun connectHeaderAndProviderOptionsUseAFlatHierarchyOnRegularWidth() {
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 1f)) {
                Box(modifier = Modifier.width(500.dp)) {
                    ConnectorsContent()
                }
            }
        }

        composeRule.onNodeWithTag("connect-hero-logo").assertDoesNotExist()
        composeRule.onNodeWithTag("provider-setup-panel").assertExists()
        composeRule.onNodeWithTag("provider-option-antigravity").assertExists()
        composeRule.onNodeWithTag("provider-option-codex").assertExists()

        val panel = composeRule.onNodeWithTag("provider-setup-panel")
            .fetchSemanticsNode().boundsInRoot
        val antigravity = composeRule.onNodeWithTag("provider-option-antigravity")
            .fetchSemanticsNode().boundsInRoot
        val codex = composeRule.onNodeWithTag("provider-option-codex")
            .fetchSemanticsNode().boundsInRoot
        assertTrue("Provider options stay within one shared panel", antigravity.left >= panel.left && antigravity.right <= panel.right)
        assertTrue("The second provider stays within the shared panel", codex.left >= panel.left && codex.right <= panel.right)
        assertTrue("Provider options remain separate sibling groups", antigravity.bottom < codex.top)
    }

    @Test
    fun providerChoiceComesBeforePrerequisitesAndBothSectionsAreHeadings() {
        composeRule.setContent {
            Box(modifier = Modifier.width(390.dp).height(1800.dp)) {
                ConnectorsContent()
            }
        }

        val providerHeading = composeRule.onNodeWithText("Choose a provider")
        composeRule.onNodeWithText("How connection works").performClick()
        val prerequisiteHeading = composeRule.onNodeWithText("Before you connect")
        providerHeading.assertIsDisplayed()
        prerequisiteHeading.assertIsDisplayed()
        assertTrue(
            "Provider choices should appear before setup prerequisites",
            providerHeading.fetchSemanticsNode().boundsInRoot.center.y <
                prerequisiteHeading.fetchSemanticsNode().boundsInRoot.center.y
        )
        assertTrue(
            "Provider choices should be announced as a heading",
            providerHeading.fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
        assertTrue(
            "Prerequisites should be announced as a heading",
            prerequisiteHeading.fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
    }
}
