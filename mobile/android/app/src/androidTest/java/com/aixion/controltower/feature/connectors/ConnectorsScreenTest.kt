package com.aixion.controltower.feature.connectors

import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeUp
import com.aixion.controltower.core.api.dto.ConnectorCreateDto
import com.aixion.controltower.core.api.dto.ConnectorDto
import com.aixion.controltower.core.api.dto.ConnectorSchemaMapperDto
import com.aixion.controltower.core.api.dto.ConnectorTemplateDto
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import java.util.concurrent.CopyOnWriteArrayList

class ConnectorsScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun firstConnectionDefaultsToAntigravityThenCodex() {
        val codex = template().copy(id = "codex", provider_label = "CODEX")
        val antigravity = template().copy(id = "antigravity", provider_label = "ANTIGRAVITY")

        assertEquals("antigravity", preferredConnectorTemplateId(listOf(codex, antigravity)))
        assertEquals("codex", preferredConnectorTemplateId(listOf(codex)))
    }

    @Test
    fun rendersConnectorConsoleTemplateCredentialAndActions() {
        composeRule.setContent {
            ConnectorsContent(
                state = ConnectorsUiState(
                    loading = false,
                    connectors = listOf(connector(secretConfigured = true)),
                    templates = listOf(template()),
                    selectedTemplateId = "openclaw",
                    apiBaseUrl = "https://aixion.example.com",
                    lastSecret = "aixion-secret-once",
                    preview = "accepted=true, auth=true, scope=true"
                )
            )
        }

        composeRule.onNodeWithText("Connect a platform").assertExists()
        composeRule.onNodeWithText(
            "Set up the coding agent running on your computer. The computer must stay on and connected while a request is waiting. Antigravity is the first verified target; Codex hook setup is available, but its live approval flow is not verified."
        ).assertExists()
        composeRule.onNodeWithText("Other integrations and webhooks").assertExists()
        composeRule.onAllNodesWithText("Create connection").assertCountEquals(0)
        composeRule.onNodeWithText("Other integrations and webhooks").performClick()
        composeRule.onNodeWithText("Create connection").assertExists()
        composeRule.onNodeWithText("OpenClaw").assertExists()
        composeRule.onNodeWithText("One-time connector credential").assertExists()
        composeRule.onNodeWithText("aixion-secret-once").assertExists()
        composeRule.onNodeWithText("accepted=true, auth=true, scope=true").assertExists()
        repeat(5) {
            composeRule.onRoot().performTouchInput { swipeUp() }
            composeRule.waitForIdle()
        }
        composeRule.onNodeWithText("OpenClaw connector").assertExists()
        composeRule.onNodeWithText("HEALTHY").assertExists()
        composeRule.onNodeWithText("https://aixion.example.com/connectors/connector_1/webhook").assertExists()
        composeRule.onNodeWithText("Advanced settings").assertExists()
        composeRule.onAllNodesWithText("Apply selected mapper").assertCountEquals(0)
        composeRule.onAllNodesWithText("Authentication failures: 0").assertCountEquals(0)
    }

    @Test
    fun nativeAntigravitySetupIsSeparateFromWebhookIntakeAndNavigates() {
        var navigationRequested = false
        composeRule.setContent {
            ConnectorsContent(
                state = ConnectorsUiState(loading = false),
                onOpenAgentAccess = { navigationRequested = true }
            )
        }

        composeRule.onNodeWithText("Antigravity approval setup").assertExists()
        composeRule.onNodeWithText("Set up Antigravity").assertExists().performClick()
        composeRule.onNodeWithText("Codex permission setup").assertExists()
        composeRule.onNodeWithText("A local PermissionRequest hook can be configured. Same-invocation phone approve/deny has not been verified yet.").assertExists()
        composeRule.onNodeWithText("Open platform setup").assertExists().performClick()
        composeRule.onAllNodesWithText("Webhook task intake").assertCountEquals(0)
        composeRule.onNodeWithText("Other integrations and webhooks").performClick()
        composeRule.onNodeWithText("Webhook task intake").assertExists()
        composeRule.onNodeWithText(
            "These integrations submit task requests. They do not install native approval hooks or resume a paused agent session."
        ).assertExists()
        composeRule.onNodeWithText("Native Codex permission approvals are not connected.").assertExists()
        composeRule.runOnIdle { assertEquals(true, navigationRequested) }
    }

    @Test
    fun connectorActionsCallExpectedCallbacks() {
        val calls = CopyOnWriteArrayList<String>()
        val connector = connector(secretConfigured = true)

        composeRule.setContent {
            ConnectorsContent(
                state = ConnectorsUiState(
                    loading = false,
                    connectors = listOf(connector),
                    templates = listOf(template()),
                    selectedTemplateId = "openclaw",
                    apiBaseUrl = "https://aixion.example.com"
                ),
                onCopy = { label, _ -> calls.add("copy:$label") },
                onToggle = { calls.add("toggle:${it.id}") },
                onIssueSecret = { calls.add("secret:${it.id}") },
                onRevokeSecret = { calls.add("revoke:${it.id}") },
                onApplyMapper = { calls.add("mapper:${it.id}") },
                onPreviewMapper = { calls.add("preview:${it.id}") }
            )
        }
        composeRule.waitForIdle()

        composeRule.onNodeWithText("Other integrations and webhooks").performScrollTo().performClick()
        composeRule.onNodeWithText("Copy webhook").performScrollTo().performClick()
        composeRule.onAllNodesWithText("Create connection credential").assertCountEquals(0)
        composeRule.onNodeWithText("Advanced settings").performScrollTo().performClick()
        composeRule.onNodeWithText("Disable connection").performScrollTo().performClick()
        composeRule.onNodeWithText("Rotate credential").performScrollTo().performClick()
        composeRule.onNodeWithText("Revoke credential").assertIsEnabled().performScrollTo().performClick()
        composeRule.onNodeWithText("Apply selected mapper").assertIsEnabled().performScrollTo().performClick()
        composeRule.onNodeWithText("Test selected template payload").assertIsEnabled().performScrollTo().performClick()

        composeRule.runOnIdle {
            assertEquals(
                listOf(
                    "copy:Webhook URL",
                    "toggle:connector_1",
                    "secret:connector_1",
                    "revoke:connector_1",
                    "mapper:connector_1",
                    "preview:connector_1"
                ),
                calls.toList()
            )
        }
    }

    @Test
    fun rendersErrorAndLoadingStates() {
        composeRule.setContent {
            ConnectorsContent(
                state = ConnectorsUiState(
                    loading = true,
                    error = "Failed to load connectors"
                )
            )
        }

        composeRule.onNodeWithText("Loading connections...").assertExists()
        composeRule.onNodeWithText("Failed to load connectors").assertExists()
        composeRule.onNodeWithText("Other integrations and webhooks").assertExists()
        composeRule.onAllNodesWithText("Create connection").assertCountEquals(0)
    }
}

private fun connector(secretConfigured: Boolean): ConnectorDto {
    return ConnectorDto(
        id = "connector_1",
        name = "OpenClaw connector",
        connector_type = "WEBHOOK",
        provider_label = "OPENCLAW",
        auth_type = "HMAC",
        status = "ENABLED",
        health_status = "HEALTHY",
        allowed_project_ids = listOf("project_1"),
        allowed_repositories = listOf("ramgolladi1503-sys/aixion-control-tower"),
        allowed_actions = listOf("CREATE_AGENT_TASK"),
        secret_configured = secretConfigured,
        failed_auth_count = 0,
    )
}

private fun template(): ConnectorTemplateDto {
    return ConnectorTemplateDto(
        id = "openclaw",
        display_name = "OpenClaw",
        description = "OpenClaw task webhook preset",
        provider_label = "OPENCLAW",
        connector_type = "WEBHOOK",
        auth_type = "HMAC",
        connector_defaults = ConnectorCreateDto(
            name = "OpenClaw connector",
            connector_type = "WEBHOOK",
            provider_label = "OPENCLAW",
            auth_type = "HMAC",
            allowed_project_ids = listOf("project_1"),
            allowed_repositories = listOf("ramgolladi1503-sys/aixion-control-tower"),
            allowed_actions = listOf("CREATE_AGENT_TASK")
        ),
        mapper = ConnectorSchemaMapperDto(
            enabled = true,
            default_action = "CREATE_AGENT_TASK",
            field_paths = mapOf("title" to "task.title"),
            defaults = mapOf("requested_action" to "CREATE_APPROVAL")
        ),
        sample_payload = mapOf("task" to mapOf("title" to "Fix tests")),
        setup_notes = listOf("Paste webhook URL into OpenClaw."),
        tags = listOf("webhook")
    )
}
