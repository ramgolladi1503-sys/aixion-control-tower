package com.aixion.controltower.feature.connectors

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.semantics.SemanticsProperties
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
        composeRule.onNodeWithText("Connect your coding agent").assertExists()
        composeRule.onNodeWithText("Before you connect").assertExists()
        composeRule.onNodeWithText("Open the project on its Mac").assertExists()
        composeRule.onNodeWithText(
            "Use the computer and agent session that should pause for approvals."
        ).assertExists()
        composeRule.onNodeWithText("Keep it reachable").assertExists()
        composeRule.onNodeWithText(
            "The Mac, agent session, and Control Tower API must stay available while a request is waiting."
        ).assertExists()
        composeRule.onNodeWithText("Save the one-time token").assertExists()
        composeRule.onNodeWithText(
            "Setup shows the connector token once. Store it like a password."
        ).assertExists()
        composeRule.onNodeWithText(
            "The phone cannot wake or restart the Mac. This screen does not verify hook installation."
        ).assertExists()
        composeRule.onAllNodesWithText("CONNECT ONCE").assertCountEquals(0)
        composeRule.onNodeWithText("Choose a provider").assertExists()
        composeRule.onNodeWithText("Antigravity").assertExists()
        composeRule.onNodeWithText("Codex").performScrollTo().assertExists()
        composeRule.onNodeWithText("Set up Antigravity").assertExists()
        composeRule.onNodeWithText(
            "A local phone approval flow passed on an earlier build. This build has not yet been verified on a phone, and delivery while away from the Mac is unverified."
        ).performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Set up Codex").performScrollTo().assertExists()
        composeRule.onNodeWithText(
            "Phone approvals from Codex Desktop and returning a decision to its same task have not been verified. Do not rely on Codex Desktop approvals yet."
        ).performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Earlier build tested").assertExists()
        composeRule.onNodeWithText("Earlier build tested").performScrollTo().assertIsDisplayed()
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

        composeRule.onNodeWithText("Earlier build tested").assertExists()
        composeRule.onNodeWithText("Phone test passed locally").assertDoesNotExist()
        composeRule.onNodeWithText("Codex Desktop not verified").assertExists()
        composeRule.onAllNodesWithText("Antigravity connected").assertCountEquals(0)
        composeRule.onAllNodesWithText("Codex connected").assertCountEquals(0)
        composeRule.onAllNodesWithText("Connected").assertCountEquals(0)
        composeRule.onAllNodesWithText("FIRST SUPPORTED").assertCountEquals(0)
    }

    @Test
    fun setupGuidanceWrapsAtLargeFontScaleOnNarrowScreen() {
        composeRule.setContent {
            Box(modifier = Modifier.width(240.dp)) {
                CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                    ConnectorsContent()
                }
            }
        }

        val heading = composeRule.onNodeWithText("Connect your coding agent")
        heading.assertExists()
        assertTrue(
            "Connect page title should be exposed as a screen-reader heading",
            heading.fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
        composeRule.runOnIdle {
            val bounds = heading.fetchSemanticsNode().boundsInRoot
            assertTrue("Connect heading must remain within the narrow viewport", bounds.right <= 240f)
            assertTrue("Connect heading must wrap at large font scale", bounds.height > 48f)
        }

        composeRule.onNodeWithText("Choose a provider").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Earlier build tested").performScrollTo().assertIsDisplayed()
    }

    @Test
    fun providerChoiceComesBeforePrerequisitesAndBothSectionsAreHeadings() {
        composeRule.setContent {
            Box(modifier = Modifier.width(390.dp).height(1800.dp)) {
                ConnectorsContent()
            }
        }

        val providerHeading = composeRule.onNodeWithText("Choose a provider")
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
