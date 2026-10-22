package com.aixion.controltower.feature.connectors

import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test

class ConnectorsScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun showsOnlyNativeProviderSetupAndDoesNotExposeWebhookTaskIntake() {
        composeRule.setContent {
            ConnectorsContent()
        }

        composeRule.onNodeWithText("Approve desktop work from your phone").assertExists()
        composeRule.onNodeWithText(
            "Connect the coding agent on your computer. Keep that computer and the same agent session running while an approval is waiting."
        ).assertExists()
        composeRule.onNodeWithText("How phone approvals work").assertExists()
        composeRule.onNodeWithText("Set up the agent").assertExists()
        composeRule.onNodeWithText("Keep the session running").assertExists()
        composeRule.onNodeWithText("Decide from your phone").assertExists()
        composeRule.onNodeWithText("FIRST SUPPORTED").assertExists()
        composeRule.onNodeWithText("Antigravity").assertExists()
        composeRule.onNodeWithText("Set up Antigravity").assertExists()
        composeRule.onNodeWithText("PHONE FLOW NOT VERIFIED").assertExists()
        composeRule.onNodeWithText("Codex").assertExists()
        composeRule.onNodeWithText("View Codex setup").assertExists()

        composeRule.onAllNodesWithText("Other integrations and webhooks").assertCountEquals(0)
        composeRule.onAllNodesWithText("Webhook task intake").assertCountEquals(0)
        composeRule.onAllNodesWithText("Choose a webhook integration").assertCountEquals(0)
        composeRule.onAllNodesWithText("Create connection").assertCountEquals(0)
    }

    @Test
    fun bothProviderSetupActionsNavigateToPlatformSetup() {
        val navigationRequests = mutableListOf<String>()
        composeRule.setContent {
            ConnectorsContent(onOpenAgentAccess = { navigationRequests += it })
        }

        composeRule.onNodeWithText("Set up Antigravity").performClick()
        composeRule.onNodeWithText("View Codex setup").performClick()

        composeRule.runOnIdle {
            assertEquals(listOf("ANTIGRAVITY", "CODEX"), navigationRequests)
        }
    }

    @Test
    fun codexStatusDoesNotClaimVerifiedPhoneApproval() {
        composeRule.setContent {
            ConnectorsContent()
        }

        composeRule.onNodeWithText("PHONE FLOW NOT VERIFIED").assertExists()
        composeRule.onNodeWithText(
            "Setup is available, but returning a phone decision to the same Codex task has not been verified."
        ).assertExists()
        composeRule.onAllNodesWithText("Codex connected").assertCountEquals(0)
    }
}
