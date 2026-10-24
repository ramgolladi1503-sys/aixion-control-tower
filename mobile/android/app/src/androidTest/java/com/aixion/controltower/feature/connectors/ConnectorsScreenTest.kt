package com.aixion.controltower.feature.connectors

import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test

class ConnectorsScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun showsSimpleNativeProviderSetupAndDoesNotExposeWebhookTaskIntake() {
        composeRule.setContent { ConnectorsContent() }

        composeRule.onNodeWithText("Set up a project").assertExists()
        composeRule.onNodeWithText("Connect your coding agent").assertExists()
        composeRule.onNodeWithText("Before you connect").assertExists()
        composeRule.onNodeWithText("On your Mac, open the project where Antigravity or Codex runs", substring = true)
            .assertExists()
        composeRule.onNodeWithText("The setup token appears once; treat it like a password.", substring = true)
            .assertExists()
        composeRule.onNodeWithText("Keep the Mac and configured Control Tower API reachable", substring = true)
            .assertExists()
        composeRule.onNodeWithText("This screen cannot confirm that setup was installed.", substring = true)
            .assertExists()
        composeRule.onAllNodesWithText("CONNECT ONCE").assertCountEquals(0)
        composeRule.onNodeWithText("Choose a provider").assertExists()
        composeRule.onNodeWithText("Antigravity").assertExists()
        composeRule.onNodeWithText("Codex").performScrollTo().assertExists()
        composeRule.onNodeWithText("Set up Antigravity").assertExists()
        composeRule.onNodeWithText(
            "A local test returned a phone decision to the same waiting Antigravity task. Remote delivery has not been verified."
        ).performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Set up Codex").performScrollTo().assertExists()
        composeRule.onNodeWithText(
            "Phone approvals from Codex Desktop and returning a decision to its same task have not been verified. Do not rely on Codex Desktop approvals yet."
        ).performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Phone test passed locally").assertExists()
        composeRule.onAllNodesWithText("Setup available").assertCountEquals(0)
        composeRule.onNodeWithText("Codex Desktop not verified").performScrollTo().assertExists()
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
                "Your decision returns to the waiting task only while its Mac, agent session, and API remain reachable."
        composeRule.setContent { ConnectorsContent() }

        composeRule.onNodeWithText("When an approval arrives")
            .performScrollTo()
            .assertIsDisplayed()
        composeRule.onNodeWithText(explanation, useUnmergedTree = true)
            .assertExists()
            .assertTextEquals(explanation)
            .assertIsDisplayed()
    }

    @Test
    fun providerSetupActionsOpenTheMatchingPlatformSetup() {
        val navigationRequests = mutableListOf<String>()
        composeRule.setContent {
            ConnectorsContent(onOpenAgentAccess = { navigationRequests += it })
        }

        composeRule.onNodeWithText("Set up Antigravity").performScrollTo().performClick()
        composeRule.onNodeWithText("Set up Codex").performScrollTo().performClick()

        composeRule.runOnIdle {
            assertEquals(listOf("ANTIGRAVITY", "CODEX"), navigationRequests)
        }
    }

    @Test
    fun providerReadinessStatesAreDistinctAndDoNotClaimLiveConnection() {
        composeRule.setContent { ConnectorsContent() }

        composeRule.onNodeWithText("Phone test passed locally").assertExists()
        composeRule.onNodeWithText("Codex Desktop not verified").assertExists()
        composeRule.onAllNodesWithText("Antigravity connected").assertCountEquals(0)
        composeRule.onAllNodesWithText("Codex connected").assertCountEquals(0)
        composeRule.onAllNodesWithText("Connected").assertCountEquals(0)
        composeRule.onAllNodesWithText("FIRST SUPPORTED").assertCountEquals(0)
    }
}
