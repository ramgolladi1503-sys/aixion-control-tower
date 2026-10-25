package com.aixion.controltower.feature.agentaccess

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.api.dto.AgentCredentialStatusDto
import com.aixion.controltower.core.api.dto.ExternalAgentDto
import com.aixion.controltower.core.api.dto.ProjectDto
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import com.aixion.controltower.data.repository.AgentAccessSummary
import org.junit.Assert.assertTrue
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

    @Test
    fun setupProgressTitleAndBadgeStackWithinNarrowLargeTextViewport() {
        composeRule.setContent {
            Box(modifier = Modifier.width(240.dp)) {
                CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                    ControlTowerTheme {
                        AgentAccessSetupGuide(AgentAccessUiState(loading = false, projectsLoaded = true))
                    }
                }
            }
        }

        val title = composeRule.onNodeWithText("Setup progress")
        val badge = composeRule.onNodeWithText("0 of 2 phone steps")
        title.assertIsDisplayed()
        badge.assertIsDisplayed()
        composeRule.runOnIdle {
            val titleBounds = title.fetchSemanticsNode().boundsInRoot
            val badgeBounds = badge.fetchSemanticsNode().boundsInRoot
            assertTrue("Setup title must fit the viewport", titleBounds.left >= 0f && titleBounds.right <= 240f)
            assertTrue("Setup status must fit the viewport", badgeBounds.left >= 0f && badgeBounds.right <= 240f)
            assertTrue("Setup status must sit below the title at large text", badgeBounds.top >= titleBounds.bottom)
        }
    }

    @Test
    fun agentAccessHeroUsesSharedHierarchyAndWrapsAtLargeFontScale() {
        composeRule.setContent {
            Box(modifier = Modifier.width(240.dp)) {
                CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                    ControlTowerTheme {
                        AgentAccessHero(provider = "ANTIGRAVITY", isLoading = false)
                    }
                }
            }
        }

        val title = composeRule.onNodeWithText("Connect Antigravity")
        val body = composeRule.onNodeWithText(
            "Create a project-limited connector credential here, then install its local hook in that project on your Mac. Approval decisions stay in this phone app."
        )
        title.assertIsDisplayed()
        body.assertIsDisplayed()
        assertTrue(
            "The hero title should be exposed as a heading",
            title.fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
        composeRule.runOnIdle {
            val titleBounds = title.fetchSemanticsNode().boundsInRoot
            val bodyBounds = body.fetchSemanticsNode().boundsInRoot
            assertTrue("The title must wrap within the narrow viewport", titleBounds.left >= 0f && titleBounds.right <= 240f)
            assertTrue("The setup guidance must wrap within the narrow viewport", bodyBounds.left >= 0f && bodyBounds.right <= 240f)
            assertTrue("Large text should remain multi-line", bodyBounds.height > titleBounds.height)
        }
    }
}
