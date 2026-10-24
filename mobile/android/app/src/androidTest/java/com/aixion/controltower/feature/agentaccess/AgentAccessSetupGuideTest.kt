package com.aixion.controltower.feature.agentaccess

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import com.aixion.controltower.core.api.dto.AgentCredentialStatusDto
import com.aixion.controltower.core.api.dto.ExternalAgentDto
import com.aixion.controltower.core.api.dto.ProjectDto
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import com.aixion.controltower.data.repository.AgentAccessSummary
import org.junit.Rule
import org.junit.Test

class AgentAccessSetupGuideTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun firstTimeStateExplainsPhoneStepsAndMacVerificationLimit() {
        composeRule.setContent {
            ControlTowerTheme {
                AgentAccessSetupGuide(
                    AgentAccessUiState(loading = false, projectsLoaded = true)
                )
            }
        }

        composeRule.onNodeWithText("0 of 2 phone steps").assertIsDisplayed()
        composeRule.onNodeWithText("Select an existing project or create one below.").assertExists()
        composeRule.onNodeWithText("Create it below. The token is shown once for secure installation on your Mac.")
            .assertExists()
        composeRule.onNodeWithText("Mac setup not confirmed").assertExists()
        composeRule.onNodeWithText(
            "This app cannot confirm hook installation or same-session resume.",
            substring = true
        )
            .assertExists()
    }

    @Test
    fun activeCredentialForSelectedProjectCompletesPhoneStepsOnly() {
        val project = ProjectDto(id = "project-1", name = "Research")
        val agent = ExternalAgentDto(
            id = "agent-1",
            provider = "ANTIGRAVITY",
            display_name = "Antigravity on this Mac",
            auth_type = "API_KEY",
            allowed_project_ids = listOf(project.id)
        )
        val credential = AgentCredentialStatusDto(
            agent_id = agent.id,
            auth_type = "API_KEY",
            credential_state = "ACTIVE",
            token_present = true
        )
        composeRule.setContent {
            ControlTowerTheme {
                AgentAccessSetupGuide(
                    AgentAccessUiState(
                        loading = false,
                        projects = listOf(project),
                        projectsLoaded = true,
                        selectedProjectId = project.id,
                        agents = listOf(AgentAccessSummary(agent, credential))
                    )
                )
            }
        }

        composeRule.onNodeWithText("2 of 2 phone steps").assertIsDisplayed()
        composeRule.onNodeWithText("Selected: Research").assertExists()
        composeRule.onNodeWithText("An active project-scoped credential is available.").assertExists()
        composeRule.onNodeWithText("Mac setup not confirmed").assertExists()
    }

    @Test
    fun revokedCredentialDoesNotCountAsCompleteAndCodexCaveatIsShown() {
        val project = ProjectDto(id = "project-1", name = "Research")
        val agent = ExternalAgentDto(
            id = "agent-1",
            provider = "CODEX",
            display_name = "Codex on this Mac",
            auth_type = "API_KEY",
            allowed_project_ids = listOf(project.id)
        )
        val credential = AgentCredentialStatusDto(
            agent_id = agent.id,
            auth_type = "API_KEY",
            credential_state = "REVOKED",
            token_present = false
        )
        composeRule.setContent {
            ControlTowerTheme {
                AgentAccessSetupGuide(
                    AgentAccessUiState(
                        loading = false,
                        selectedProvider = "CODEX",
                        projects = listOf(project),
                        projectsLoaded = true,
                        selectedProjectId = project.id,
                        agents = listOf(AgentAccessSummary(agent, credential))
                    )
                )
            }
        }

        composeRule.onNodeWithText("1 of 2 phone steps").assertIsDisplayed()
        composeRule.onNodeWithText("Native Codex Desktop approval interception is not verified.", substring = true)
            .assertExists()
        composeRule.onNodeWithText("Mac setup not confirmed").assertExists()
    }
}
