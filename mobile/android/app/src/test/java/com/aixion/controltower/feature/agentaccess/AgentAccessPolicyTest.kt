package com.aixion.controltower.feature.agentaccess

import com.aixion.controltower.core.api.dto.AgentCreateRequestDto
import com.aixion.controltower.core.api.dto.ExternalAgentDto
import com.aixion.controltower.core.api.dto.ANTIGRAVITY_APPROVAL_ACTIONS
import com.aixion.controltower.core.api.dto.isExpectedAntigravityApprovalAgent
import com.aixion.controltower.data.repository.AgentAccessSummary
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class AgentAccessPolicyTest {
    @Test
    fun newAntigravityCredentialIsBoundToOneProjectAndApprovalOnlyActions() {
        val request = AgentCreateRequestDto(
            display_name = "Antigravity test Mac",
            allowed_project_ids = listOf("project-1")
        )

        assertEquals("ANTIGRAVITY", request.provider)
        assertEquals("API_KEY", request.auth_type)
        assertEquals(listOf("project-1"), request.allowed_project_ids)
        assertEquals(
            listOf(
                "CREATE_APPROVAL",
                "READ_APPROVAL_DECISION",
                "CONSUME_APPROVAL_DECISION",
                "REPORT_APPROVAL_DELIVERY_FAILURE",
                "EXPIRE_OWN_APPROVAL"
            ),
            request.allowed_actions
        )
        assertEquals(ANTIGRAVITY_APPROVAL_ACTIONS, request.allowed_actions)
        assertFalse(request.allowed_actions.contains("EXECUTE_GITHUB"))
        assertFalse(request.allowed_actions.contains("APPROVE"))
    }

    @Test
    fun missingCredentialStatusRemainsUnknownInsteadOfShowingEnabledAsActive() {
        val summary = AgentAccessSummary(
            agent = ExternalAgentDto(
                id = "agent-1",
                provider = "ANTIGRAVITY",
                display_name = "Mac",
                auth_type = "API_KEY",
                enabled = true
            ),
            credential = null
        )

        assertEquals("UNKNOWN", summary.credentialState)
    }

    @Test
    fun onlyExactAntigravityProjectAndActionScopeIsAccepted() {
        val agent = ExternalAgentDto(
            id = "agent-1", provider = "ANTIGRAVITY", display_name = "Mac", auth_type = "API_KEY",
            allowed_project_ids = listOf("project-1"), allowed_actions = ANTIGRAVITY_APPROVAL_ACTIONS
        )
        assertEquals(true, isExpectedAntigravityApprovalAgent(agent, "project-1"))
        assertFalse(isExpectedAntigravityApprovalAgent(agent.copy(allowed_project_ids = listOf("project-1", "project-2")), "project-1"))
        assertFalse(isExpectedAntigravityApprovalAgent(agent.copy(allowed_actions = ANTIGRAVITY_APPROVAL_ACTIONS + "EXECUTE_GITHUB"), "project-1"))
        assertFalse(isExpectedAntigravityApprovalAgent(agent.copy(allowed_actions = ANTIGRAVITY_APPROVAL_ACTIONS.dropLast(1)), "project-1"))
        assertFalse(isExpectedAntigravityApprovalAgent(agent.copy(allowed_actions = ANTIGRAVITY_APPROVAL_ACTIONS + "APPROVE"), "project-1"))
        assertFalse(isExpectedAntigravityApprovalAgent(agent.copy(allowed_actions = ANTIGRAVITY_APPROVAL_ACTIONS.dropLast(1) + "CREATE_APPROVAL"), "project-1"))
        assertFalse(isExpectedAntigravityApprovalAgent(agent.copy(provider = "CODEX"), "project-1"))
        assertFalse(isExpectedAntigravityApprovalAgent(agent.copy(auth_type = "OAUTH"), "project-1"))
    }
}
