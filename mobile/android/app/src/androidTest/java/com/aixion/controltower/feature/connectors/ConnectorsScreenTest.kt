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

        composeRule.onNodeWithText("Connect a platform").assertExists()
        composeRule.onNodeWithText(
            "Set up the coding agent running on your computer. The computer and the same agent session must stay on while a request is waiting. Antigravity is the first supported target; Codex phone approval is not verified yet."
        ).assertExists()
        composeRule.onNodeWithText("Antigravity approval setup").assertExists()
        composeRule.onNodeWithText("Set up Antigravity").assertExists()
        composeRule.onNodeWithText("Codex permission setup").assertExists()
        composeRule.onNodeWithText("Open platform setup").assertExists()

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
        composeRule.onNodeWithText("Open platform setup").performClick()

        composeRule.runOnIdle {
            assertEquals(listOf("ANTIGRAVITY", "CODEX"), navigationRequests)
        }
    }

    @Test
    fun codexStatusDoesNotClaimVerifiedPhoneApproval() {
        composeRule.setContent {
            ConnectorsContent()
        }

        composeRule.onNodeWithText("CODEX FLOW UNVERIFIED").assertExists()
        composeRule.onNodeWithText(
            "A local PermissionRequest hook can be configured. Same-invocation phone approve/deny has not been verified yet."
        ).assertExists()
    }
}
