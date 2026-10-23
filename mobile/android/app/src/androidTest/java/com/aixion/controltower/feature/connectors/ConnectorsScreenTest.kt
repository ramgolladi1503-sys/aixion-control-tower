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
        composeRule.onAllNodesWithText("CONNECT ONCE").assertCountEquals(0)
        composeRule.onNodeWithText("Choose a provider").assertExists()
        composeRule.onNodeWithText("Antigravity").assertExists()
        composeRule.onNodeWithText("Codex").performScrollTo().assertExists()
        composeRule.onNodeWithText("Set up Antigravity").assertExists()
        composeRule.onNodeWithText("Set up Codex").performScrollTo().assertExists()
        composeRule.onNodeWithText(
            "Capture local Codex permission prompts. Codex Desktop approval interception is not verified."
        ).performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Phone approval return not verified").assertExists()
        composeRule.onNodeWithText(
            "Setup is available. The same-session phone approval flow still needs live verification."
        ).performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("When an approval is needed").performScrollTo().assertExists()

        composeRule.onAllNodesWithText("Other integrations and webhooks").assertCountEquals(0)
        composeRule.onAllNodesWithText("Webhook task intake").assertCountEquals(0)
        composeRule.onAllNodesWithText("Choose a webhook integration").assertCountEquals(0)
        composeRule.onAllNodesWithText("Create connection").assertCountEquals(0)
    }

    @Test
    fun approvalFlowExplainsReviewAndSameSessionRequirement() {
        val explanation =
            "The request appears here. Review the exact action and approve or deny it. " +
                "Keep the computer and agent session running so the same task can continue."
        composeRule.setContent { ConnectorsContent() }

        composeRule.onNodeWithText(explanation, useUnmergedTree = true)
            .performScrollTo()
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
    fun connectionScreenDoesNotClaimEitherProviderIsVerified() {
        composeRule.setContent { ConnectorsContent() }

        composeRule.onNodeWithText("Phone approval return not verified").assertExists()
        composeRule.onAllNodesWithText("Antigravity connected").assertCountEquals(0)
        composeRule.onAllNodesWithText("Codex connected").assertCountEquals(0)
        composeRule.onAllNodesWithText("FIRST SUPPORTED").assertCountEquals(0)
    }
}
