package com.aixion.controltower.feature.agentaccess

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AgentAccessHostSetupTest {
    @Test
    fun providerHostSupportMatchesDocumentedAdapters() {
        assertTrue(isSupportedProviderHost("CODEX", HostPlatform.MACOS))
        assertTrue(isSupportedProviderHost("CODEX", HostPlatform.WINDOWS))
        assertTrue(isSupportedProviderHost("ANTIGRAVITY", HostPlatform.MACOS))
        assertFalse(isSupportedProviderHost("ANTIGRAVITY", HostPlatform.WINDOWS))
        assertFalse(isSupportedProviderHost("CODEX_DESKTOP", HostPlatform.MACOS))
    }

    @Test
    fun windowsPathsRequireDriveAbsoluteSyntaxAndNormalizeSegments() {
        assertEquals(
            "C:\\Users\\Madhuri\\Aixion",
            normalizeWorkspacePath("C:\\Users\\Madhuri\\Work\\..\\Aixion", HostPlatform.WINDOWS)
        )
        assertNull(normalizeWorkspacePath("relative\\project", HostPlatform.WINDOWS))
        assertNull(normalizeWorkspacePath("C:\\", HostPlatform.WINDOWS))
        assertNull(normalizeWorkspacePath("C:\\..\\outside", HostPlatform.WINDOWS))
        assertNull(normalizeWorkspacePath("C:\\safe\n--agent-id injected", HostPlatform.WINDOWS))
    }

    @Test
    fun windowsCodexSetupUsesCredentialManagerAndPowerShellQuotingWithoutSecret() {
        val command = hookInstallCommand(
            provider = "CODEX",
            workspacePath = "C:\\Users\\Madhuri\\Aixion Project",
            agentId = "agent's-id",
            projectId = "project-1",
            apiBaseUrl = "https://api.example.test/",
            host = HostPlatform.WINDOWS
        )

        assertEquals(
            "py -3 'integrations/codex/setup_workspace.py' `\n" +
                "  --workspace 'C:\\Users\\Madhuri\\Aixion Project' `\n" +
                "  --api-base-url 'https://api.example.test' `\n" +
                "  --agent-id 'agent''s-id' `\n" +
                "  --project-id 'project-1'",
            command
        )
        assertFalse(command.orEmpty().contains("one-time-secret"))
    }

    @Test
    fun antigravityWindowsNeverProducesAnInstallCommand() {
        assertNull(
            hookInstallCommand(
                provider = "ANTIGRAVITY",
                workspacePath = "C:\\Users\\Madhuri\\Project",
                agentId = "agent-1",
                projectId = "project-1",
                apiBaseUrl = "https://api.example.test",
                host = HostPlatform.WINDOWS
            )
        )
    }
}
