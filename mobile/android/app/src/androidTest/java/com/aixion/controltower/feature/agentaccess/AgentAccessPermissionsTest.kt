package com.aixion.controltower.feature.agentaccess

import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import org.junit.Rule
import org.junit.Test

class AgentAccessPermissionsTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun connectorScopeAndDecisionLimitationsRemainVisibleAsReadableContent() {
        composeRule.setContent {
            ControlTowerTheme {
                ConnectorPermissionsDisclosure()
            }
        }

        composeRule.onNodeWithText("Granted actions").assertExists()
        composeRule.onNodeWithText(
            "This connector is limited to the selected project and these five approval-only actions:"
        ).assertExists()

        listOf(
            "CREATE_APPROVAL",
            "READ_APPROVAL_DECISION",
            "CONSUME_APPROVAL_DECISION",
            "REPORT_APPROVAL_DELIVERY_FAILURE",
            "EXPIRE_OWN_APPROVAL"
        ).forEach { action ->
            composeRule.onNodeWithText("• $action").assertExists()
        }

        composeRule.onNodeWithText(
            "Consume only returns an already-approved decision once. This credential cannot approve requests or start tools; the paused host agent continues only after that decision is returned."
        ).assertTextEquals(
            "Consume only returns an already-approved decision once. This credential cannot approve requests or start tools; the paused host agent continues only after that decision is returned."
        )
    }

    @Test
    fun firstProjectFormCollectsNameAndOptionalDescriptionAndSubmits() {
        var submitted = false
        composeRule.setContent {
            var name by remember { mutableStateOf("") }
            var description by remember { mutableStateOf("") }
            ControlTowerTheme {
                FirstProjectSetupPanel(
                    name = name,
                    description = description,
                    creating = false,
                    enabled = true,
                    onNameChange = { name = it },
                    onDescriptionChange = { description = it },
                    onCreate = { submitted = true }
                )
            }
        }

        composeRule.onNodeWithText("Create your first project").assertExists()
        composeRule.onNodeWithText("Project name").performTextInput("Mobile approval E2E")
        composeRule.onNodeWithText("Description (optional)").performTextInput("Isolated test workspace")
        composeRule.onNodeWithText("Create project").performClick()

        composeRule.runOnIdle { assert(submitted) }
    }
}
