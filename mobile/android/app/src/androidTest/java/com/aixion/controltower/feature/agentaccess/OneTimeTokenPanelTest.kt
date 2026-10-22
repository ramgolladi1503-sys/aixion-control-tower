package com.aixion.controltower.feature.agentaccess

import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
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
}
