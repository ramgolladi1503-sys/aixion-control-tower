package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.dto.ConnectorCreateDto
import com.aixion.controltower.core.api.dto.ConnectorDto
import com.aixion.controltower.core.api.dto.ConnectorSchemaMapperDto
import com.aixion.controltower.core.api.dto.ConnectorSecretIssueResponseDto
import com.aixion.controltower.core.api.dto.ConnectorSecretRequestDto
import com.aixion.controltower.core.api.dto.ConnectorSimulationRequestDto
import com.aixion.controltower.core.api.dto.ConnectorSimulationResponseDto
import com.aixion.controltower.core.api.dto.ConnectorTemplateDto
import com.aixion.controltower.core.api.dto.ConnectorTemplateListDto
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ConnectorRepositoryTest {
    @Test
    fun createFromTemplateUsesTemplateConnectorDefaults() = runTest {
        val api = ConnectorApi()
        val repository = ConnectorRepository(api)
        val template = connectorTemplate()

        val created = repository.createFromTemplate(template)

        assertEquals("connector_openclaw", created.id)
        assertEquals(template.connector_defaults, api.createdPayload)
    }

    @Test
    fun issueRotateAndRevokeCredentialUseExpectedConnectorEndpoints() = runTest {
        val api = ConnectorApi()
        val repository = ConnectorRepository(api)

        val issued = repository.issueSecret("connector_1")
        val rotated = repository.rotateSecret("connector_1")
        val revoked = repository.revokeSecret("connector_1")

        assertEquals("issued-secret", issued)
        assertEquals("rotated-secret", rotated)
        assertEquals("connector_1", api.issuedConnectorId)
        assertEquals("issued from Android owner console", api.issuedPayload?.note)
        assertEquals("connector_1", api.rotatedConnectorId)
        assertEquals("rotated from Android owner console", api.rotatedPayload?.note)
        assertEquals("connector_1", api.revokedConnectorId)
        assertFalse(revoked.secret_configured)
    }

    @Test
    fun applyTemplateMapperSendsSelectedMapperToBackend() = runTest {
        val api = ConnectorApi()
        val repository = ConnectorRepository(api)
        val mapper = ConnectorSchemaMapperDto(
            enabled = true,
            default_action = "CREATE_AGENT_TASK",
            field_paths = mapOf("title" to "issue.title"),
            defaults = mapOf("repository" to "ramgolladi1503-sys/aixion-control-tower")
        )

        repository.applyTemplateMapper("connector_1", mapper)

        assertEquals("connector_1", api.mapperConnectorId)
        assertEquals(mapper, api.mapperPayload)
    }

    @Test
    fun simulateTemplatePayloadUsesTemplatePayloadAndMapperWithoutCreatingRealTask() = runTest {
        val api = ConnectorApi()
        val repository = ConnectorRepository(api)
        val template = connectorTemplate()

        val simulation = repository.simulateTemplatePayload("connector_1", template)

        assertTrue(simulation.accepted)
        assertEquals("connector_1", api.simulatedConnectorId)
        assertEquals(template.sample_payload, api.simulationPayload?.sample_payload)
        assertEquals(template.mapper, api.simulationPayload?.mapper)
        assertTrue(api.simulationPayload?.validate_secret_configured ?: false)
    }
}

private fun connectorTemplate(): ConnectorTemplateDto {
    val mapper = ConnectorSchemaMapperDto(
        enabled = true,
        default_action = "CREATE_AGENT_TASK",
        field_paths = mapOf("title" to "task.title", "goal" to "task.goal"),
        defaults = mapOf("requested_action" to "CREATE_APPROVAL")
    )
    return ConnectorTemplateDto(
        id = "openclaw",
        display_name = "OpenClaw",
        description = "OpenClaw task webhook template",
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
        mapper = mapper,
        sample_payload = mapOf("task" to mapOf("title" to "Fix tests", "goal" to "Improve regression coverage")),
        setup_notes = listOf("Paste webhook URL into OpenClaw."),
        tags = listOf("agent", "webhook")
    )
}

private class ConnectorApi : BaseFakeControlTowerApi() {
    var createdPayload: ConnectorCreateDto? = null
    var issuedConnectorId: String? = null
    var issuedPayload: ConnectorSecretRequestDto? = null
    var rotatedConnectorId: String? = null
    var rotatedPayload: ConnectorSecretRequestDto? = null
    var revokedConnectorId: String? = null
    var mapperConnectorId: String? = null
    var mapperPayload: ConnectorSchemaMapperDto? = null
    var simulatedConnectorId: String? = null
    var simulationPayload: ConnectorSimulationRequestDto? = null

    override suspend fun listConnectors(): List<ConnectorDto> = listOf(connector(secretConfigured = true))

    override suspend fun createConnector(payload: ConnectorCreateDto): ConnectorDto {
        createdPayload = payload
        return connector(id = "connector_openclaw", secretConfigured = false)
    }

    override suspend fun listConnectorTemplates(): ConnectorTemplateListDto {
        return ConnectorTemplateListDto(templates = listOf(connectorTemplate()))
    }

    override suspend fun issueConnectorSecret(
        connectorId: String,
        payload: ConnectorSecretRequestDto
    ): ConnectorSecretIssueResponseDto {
        issuedConnectorId = connectorId
        issuedPayload = payload
        return ConnectorSecretIssueResponseDto(connector = connector(secretConfigured = true), secret = "issued-secret")
    }

    override suspend fun rotateConnectorSecret(
        connectorId: String,
        payload: ConnectorSecretRequestDto
    ): ConnectorSecretIssueResponseDto {
        rotatedConnectorId = connectorId
        rotatedPayload = payload
        return ConnectorSecretIssueResponseDto(connector = connector(secretConfigured = true), secret = "rotated-secret")
    }

    override suspend fun revokeConnectorSecret(connectorId: String): ConnectorDto {
        revokedConnectorId = connectorId
        return connector(secretConfigured = false)
    }

    override suspend fun updateConnectorSchemaMapper(
        connectorId: String,
        payload: ConnectorSchemaMapperDto
    ) = com.aixion.controltower.core.api.dto.ConnectorSchemaMapperStatusDto(
        connector_id = connectorId,
        mapper = payload,
        mapper_enabled = payload.enabled
    ).also {
        mapperConnectorId = connectorId
        mapperPayload = payload
    }

    override suspend fun simulateConnectorWebhook(
        connectorId: String,
        payload: ConnectorSimulationRequestDto
    ): ConnectorSimulationResponseDto {
        simulatedConnectorId = connectorId
        simulationPayload = payload
        return ConnectorSimulationResponseDto(
            accepted = true,
            connector_id = connectorId,
            action = payload.mapper?.default_action,
            normalized_payload = mapOf("title" to "Fix tests"),
            task_preview = mapOf("title" to "Fix tests"),
            auth_ready = true,
            scope_ready = true,
        )
    }

    private fun connector(id: String = "connector_1", secretConfigured: Boolean): ConnectorDto {
        return ConnectorDto(
            id = id,
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
        )
    }
}
