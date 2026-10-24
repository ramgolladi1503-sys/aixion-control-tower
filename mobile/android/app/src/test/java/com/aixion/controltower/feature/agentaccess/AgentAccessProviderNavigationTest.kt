package com.aixion.controltower.feature.agentaccess

import com.aixion.controltower.navigation.Route
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class AgentAccessProviderNavigationTest {
    @Test
    fun routeBuilderPreservesSelectedProvider() {
        assertEquals("agent_access/antigravity", Route.AgentAccess.forProvider("ANTIGRAVITY"))
        assertEquals("agent_access/codex", Route.AgentAccess.forProvider("CODEX"))
    }

    @Test
    fun routeBuilderNormalizesProviderWithoutLocaleDependence() {
        assertEquals("agent_access/codex", Route.AgentAccess.forProvider("  codex  "))
    }

    @Test
    fun routeBuilderRejectsUnsupportedProvider() {
        assertThrows(IllegalArgumentException::class.java) {
            Route.AgentAccess.forProvider("UNKNOWN")
        }
    }

    @Test
    fun viewModelInitialStateUsesRouteProviderBeforeFirstFetch() {
        val codex = agentAccessUiStateForProvider("codex")
        assertEquals("CODEX", codex.selectedProvider)
        assertEquals("Codex on this Mac", codex.displayName)

        val antigravity = agentAccessUiStateForProvider("antigravity")
        assertEquals("ANTIGRAVITY", antigravity.selectedProvider)
        assertEquals("Antigravity on this Mac", antigravity.displayName)
    }

    @Test
    fun missingOrUnexpectedRouteProviderDefaultsToAntigravity() {
        assertEquals("ANTIGRAVITY", agentAccessUiStateForProvider(null).selectedProvider)
        assertEquals("ANTIGRAVITY", agentAccessUiStateForProvider("UNKNOWN").selectedProvider)
    }
}
