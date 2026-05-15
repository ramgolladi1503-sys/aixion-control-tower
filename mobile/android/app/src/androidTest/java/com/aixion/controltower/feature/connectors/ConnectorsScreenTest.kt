package com.aixion.controltower.feature.connectors

import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import com.aixion.controltower.core.api.dto.ConnectorCreateDto
import com.aixion.controltower.core.api.dto.ConnectorDto
import com.aixion.controltower.core.api.dto.ConnectorSchemaMapperDto
import com.aixion.controltower.core.api.dto.ConnectorTemplateDto
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test

class ConnectorsScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

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

        composeRule.onNodeWithText("Connectors").assertExists()
        composeRule.onNodeWithText("Bring-your-own-agent control from phone.").assertExists()
        composeRule.onNodeWithText("Templates").assertExists()
        composeRule.onNodeWithText("✓ OpenClaw").assertExists()
        composeRule.onNodeWithText("OpenClaw connector").assertExists()
        composeRule.onNodeWithText("Credential set").assertExists()
        composeRule.onNodeWithText("https://aixion.example.com/connectors/connector_1/webhook").assertExists()
        composeRule.onNodeWithText("One-time connector credential").assertExists()
        composeRule.onNodeWithText("aixion-secret-once").assertExists()
        composeRule.onNodeWithText("accepted=true, auth=true, scope=true").assertExists()
    }

    @Test
    fun connectorActionsCallExpectedCallbacks() {
        val calls = mutableListOf<String>()
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

        composeRule.onNodeWithText("Copy webhook").performClick()
        composeRule.onNodeWithText("Disable").performClick()
        composeRule.onNodeWithText("Rotate credential").performClick()
        composeRule.onNodeWithText("Revoke credential").assertIsEnabled().performClick()
        composeRule.onNodeWithText("Apply mapper").assertIsEnabled().performClick()
        composeRule.onNodeWithText("Test selected template payload").assertIsEnabled().performClick()

        assertEquals(
            listOf(
                "copy:Webhook URL",
                "toggle:connector_1",
                "secret:connector_1",
                "revoke:connector_1",
                "mapper:connector_1",
                "preview:connector_1"
            ),
            calls
        )
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

        composeRule.onNodeWithText("Loading connector console...").assertExists()
        composeRule.onNodeWithText("Failed to load connectors").assertExists()
        composeRule.onNodeWithText("Create connector from selected template").assertExists()
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
