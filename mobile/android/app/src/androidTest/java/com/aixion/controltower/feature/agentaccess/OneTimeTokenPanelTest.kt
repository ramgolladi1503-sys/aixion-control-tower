package com.aixion.controltower.feature.agentaccess

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class OneTimeTokenPanelTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun setupInstructionsKeepTheFullCommandAndSecretHandlingGuidance() {
        composeRule.setContent {
            ControlTowerTheme {
                OneTimeTokenPanel(
                    token = "one-time-token-example",
                    provider = "ANTIGRAVITY",
                    agentName = "Antigravity connector",
                    agentId = "agent-example",
                    projectId = "project-example",
                    onCopy = {},
                    onHide = {}
                )
            }
        }

        composeRule.onNodeWithText(
            "It is held only in screen memory and is cleared when you hide it or leave the app."
        ).assertExists()
        composeRule.onNodeWithText(
            "On the Mac, run: security add-generic-password -U -s com.aixion.controltower.agent -a agent-example -w\nThe command prompts for the token; do not put it in command arguments or shell history."
        ).assertExists()
        composeRule.onNodeWithText(
            "python3 /ABSOLUTE/PATH/integrations/antigravity/setup_workspace.py \\\n  --workspace \"/ABSOLUTE/PATH/TO/PROJECT\" \\\n  --api-base-url \"https://CONTROL-TOWER-URL\" \\\n  --agent-id \"agent-example\" \\\n  --project-id \"project-example\""
        ).assertExists()
    }

    @Test
    fun tokenActionsStayWithinNarrowLargeTextLayout() {
        composeRule.setContent {
            Box(modifier = Modifier.width(240.dp)) {
                CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 1.5f)) {
                    ControlTowerTheme {
                        OneTimeTokenPanel(
                            token = "one-time-token-example",
                            provider = "ANTIGRAVITY",
                            agentName = "Antigravity connector",
                            agentId = "agent-example",
                            projectId = "project-example",
                            onCopy = {},
                            onHide = {}
                        )
                    }
                }
            }
        }

        val copyAction = composeRule.onNodeWithText("Copy sensitive token")
        val hideAction = composeRule.onNodeWithText("Hide now")
        copyAction.assertExists()
        hideAction.assertExists()
        composeRule.runOnIdle {
            assertTrue(
                "Copy action should remain within the narrow viewport",
                copyAction.fetchSemanticsNode().boundsInRoot.right <= 240f
            )
            assertTrue(
                "Hide action should remain within the narrow viewport",
                hideAction.fetchSemanticsNode().boundsInRoot.right <= 240f
            )
        }
    }
}
