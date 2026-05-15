package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.ConnectorCreateDto
import com.aixion.controltower.core.api.dto.ConnectorDto
import com.aixion.controltower.core.api.dto.ConnectorSchemaMapperDto
import com.aixion.controltower.core.api.dto.ConnectorSchemaMapperPreviewRequestDto
import com.aixion.controltower.core.api.dto.ConnectorSchemaMapperPreviewResponseDto
import com.aixion.controltower.core.api.dto.ConnectorSecretRequestDto
import com.aixion.controltower.core.api.dto.ConnectorTemplateDto

class ConnectorRepository(private val api: ControlTowerApi) {
    suspend fun listConnectors(): List<ConnectorDto> = api.listConnectors()

    suspend fun listTemplates(): List<ConnectorTemplateDto> = api.listConnectorTemplates().templates

    suspend fun createFromTemplate(template: ConnectorTemplateDto): ConnectorDto {
        return api.createConnector(template.connector_defaults)
    }

    suspend fun enable(connectorId: String): ConnectorDto = api.enableConnector(connectorId)

    suspend fun disable(connectorId: String): ConnectorDto = api.disableConnector(connectorId)

    suspend fun issueSecret(connectorId: String): String {
        return api.issueConnectorSecret(connectorId, ConnectorSecretRequestDto("issued from Android owner console")).secret.orEmpty()
    }

    suspend fun rotateSecret(connectorId: String): String {
        return api.rotateConnectorSecret(connectorId, ConnectorSecretRequestDto("rotated from Android owner console")).secret.orEmpty()
    }

    suspend fun revokeSecret(connectorId: String): ConnectorDto = api.revokeConnectorSecret(connectorId)

    suspend fun applyTemplateMapper(connectorId: String, mapper: ConnectorSchemaMapperDto) {
        api.updateConnectorSchemaMapper(connectorId, mapper)
    }

    suspend fun previewMapper(connectorId: String, template: ConnectorTemplateDto): ConnectorSchemaMapperPreviewResponseDto {
        return api.previewConnectorSchemaMapper(
            connectorId,
            ConnectorSchemaMapperPreviewRequestDto(sample_payload = template.sample_payload, mapper = template.mapper)
        )
    }
}
