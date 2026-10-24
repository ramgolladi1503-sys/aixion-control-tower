package com.aixion.controltower.feature.agentaccess

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import com.aixion.controltower.BuildConfig
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class OneTimeTokenPanelTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun newlyIssuedCredentialIsAnnouncedWithoutReadingTheSecretAloud() {
        composeRule.setContent {
            ControlTowerTheme {
                OneTimeTokenPanel(
                    token = "one-time-token-example",
                    provider = "ANTIGRAVITY",
                    agentName = "Antigravity connector",
                    agentId = "agent-example",
                    projectId = "project-example",
                    apiBaseUrl = "https://api.example.test/",
                    onCopy = {},
                    onCopyKeychainCommand = { true },
                    onCopyHookInstallCommand = { true },
                    onHide = {}
                )
            }
        }

        val announcement = composeRule.onNodeWithTag("agent-token-created-announcement")
        announcement.assertExists()
        assertEquals(
            LiveRegionMode.Polite,
            announcement.fetchSemanticsNode().config[SemanticsProperties.LiveRegion]
        )
        composeRule.onNodeWithText("one-time-token-example").assertExists()
        assertFalse(
            announcement.fetchSemanticsNode().config.getOrNull(SemanticsProperties.ContentDescription)
                ?.any { it.contains("one-time-token-example") } == true
        )
    }

    @Test
    fun setupInstructionsUseRealRepositoryRelativeCommandAndNoPlaceholders() {
        composeRule.setContent {
            ControlTowerTheme {
                OneTimeTokenPanel(
                    token = "one-time-token-example",
                    provider = "ANTIGRAVITY",
                    agentName = "Antigravity connector",
                    agentId = "agent-example",
                    projectId = "project-example",
                    apiBaseUrl = "http://10.0.2.2:8000/",
                    onCopy = {},
                    onCopyKeychainCommand = { true },
                    onCopyHookInstallCommand = { true },
                    onHide = {}
                )
            }
        }

        composeRule.onNodeWithText(
            "It is held only in screen memory and is cleared when you hide it or leave the app."
        ).assertExists()
        composeRule.onNodeWithText("One-time secret").assertExists()
        composeRule.onNodeWithText(
            "Store the token in macOS Keychain. This command contains no token; Terminal prompts you to enter it securely."
        ).assertExists()
        composeRule.onNodeWithText(
            "security add-generic-password -U -s com.aixion.controltower.agent -a 'agent-example' -w"
        ).assertExists()
        composeRule.onNodeWithText("Copy Keychain command").assertExists()
        composeRule.onNodeWithText(
            "Run the command from the Control Tower repository root, with this local hook repository available.",
            substring = true
        ).assertExists()
        composeRule.onNodeWithText("Absolute local project directory").assertExists()
        composeRule.onNodeWithText(
            "This app build's API URL cannot be used from the Mac.",
            substring = true
        ).assertExists()
        composeRule.onNodeWithText("Absolute local project directory")
            .performTextInput("/Users/test/Projects/example")
        composeRule.onNodeWithText("Copy hook install command").assertIsNotEnabled()
        composeRule.onNodeWithText("/ABSOLUTE/PATH", substring = true).assertDoesNotExist()
        composeRule.onNodeWithText("CONTROL-TOWER-URL", substring = true).assertDoesNotExist()
        composeRule.onNodeWithText("python3 'integrations/antigravity/setup_workspace.py'", substring = true)
            .assertDoesNotExist()
    }

    @Test
    fun invalidWorkspacePathKeepsHookCommandUnavailableAndCopyDisabled() {
        composeRule.setContent {
            ControlTowerTheme {
                OneTimeTokenPanel(
                    token = "secret-example",
                    provider = "ANTIGRAVITY",
                    agentName = "Antigravity connector",
                    agentId = "agent-example",
                    projectId = "project-example",
                    apiBaseUrl = "https://api.example.test/",
                    onCopy = {},
                    onCopyKeychainCommand = { true },
                    onCopyHookInstallCommand = { error("Invalid path must never be copied") },
                    onHide = {}
                )
            }
        }

        composeRule.onNodeWithText("Absolute local project directory")
            .performTextInput("relative/project")
        composeRule.onNodeWithText(
            "Enter a valid absolute project path, such as /Users/you/Projects/example."
        ).assertExists()
        composeRule.onNodeWithText("Copy hook install command").assertIsNotEnabled()
        composeRule.onNodeWithText("python3 'integrations/antigravity/setup_workspace.py'", substring = true)
            .assertDoesNotExist()
    }

    @Test
    fun validPathBuildsQuotedCommandWithConfiguredUrlAndNoToken() {
        var hookCommandCopied: String? = null
        val path = "/Users/test/Projects/it's a project"
        composeRule.setContent {
            ControlTowerTheme {
                OneTimeTokenPanel(
                    token = "secret-example",
                    provider = "ANTIGRAVITY",
                    agentName = "Antigravity connector",
                    agentId = "agent-example",
                    projectId = "project-example",
                    apiBaseUrl = "https://api.example.test/",
                    onCopy = {},
                    onCopyKeychainCommand = { true },
                    onCopyHookInstallCommand = { command ->
                        hookCommandCopied = command
                        true
                    },
                    onHide = {}
                )
            }
        }

        composeRule.onNodeWithText("Absolute local project directory").performTextInput(path)
        val expected = "python3 'integrations/antigravity/setup_workspace.py' \\\n" +
            "  --workspace '/Users/test/Projects/it'\\''s a project' \\\n" +
            "  --api-base-url 'https://api.example.test' \\\n" +
            "  --agent-id 'agent-example' \\\n" +
            "  --project-id 'project-example'"

        composeRule.onNodeWithText(expected).assertExists()
        composeRule.onNodeWithText("Copy hook install command").performClick()
        composeRule.onNodeWithText(
            "Hook install command copied. Run it from the Control Tower repository root."
        ).assertExists()
        assertEquals(expected, hookCommandCopied)
        assertFalse(hookCommandCopied.orEmpty().contains("secret-example"))
        assertFalse(hookCommandCopied.orEmpty().contains("/ABSOLUTE/PATH"))
        assertFalse(hookCommandCopied.orEmpty().contains("CONTROL-TOWER-URL"))
        assertTrue(hookCommandCopied.orEmpty().contains("'https://api.example.test'"))
    }

    @Test
    fun pathNormalizationRequiresAbsoluteNonRootPath() {
        assertNull(normalizeWorkspacePath("relative/project"))
        assertNull(normalizeWorkspacePath("/"))
        assertNull(normalizeWorkspacePath(" /Users/test/project"))
        assertNull(normalizeWorkspacePath("/Users/test/project "))
        assertNull(normalizeWorkspacePath("/Users/test/Project\nInjected"))
        assertEquals("/Users/test/project", normalizeWorkspacePath("/Users/test/./folder/../project"))
        assertTrue(
            hookInstallCommand("CODEX", "/Users/test/project", "a", "p", "https://api.example.test/")
                ?.contains("integrations/codex/setup_workspace.py") == true
        )
        val buildConfiguredCommand = hookInstallCommand("CODEX", "/Users/test/project", "a", "p")
        if (isMacCompatibleApiBaseUrl(BuildConfig.AIXION_API_BASE_URL)) {
            assertTrue(
                buildConfiguredCommand.orEmpty()
                    .contains(BuildConfig.AIXION_API_BASE_URL.trimEnd('/'))
            )
        } else {
            assertNull(buildConfiguredCommand)
        }
    }

    @Test
    fun apiUrlMustBeUsableFromMacAndMatchHookScriptPolicy() {
        assertFalse(isMacCompatibleApiBaseUrl("http://10.0.2.2:8000/"))
        assertFalse(isMacCompatibleApiBaseUrl("http://192.168.1.20:8000/"))
        assertFalse(isMacCompatibleApiBaseUrl("https://user:pass@api.example.test/"))
        assertFalse(isMacCompatibleApiBaseUrl("https://api.example.test/?token=secret"))
        assertFalse(isMacCompatibleApiBaseUrl("https://api.example.test:65536/"))
        assertFalse(isMacCompatibleApiBaseUrl("https://api.example.test:invalid/"))
        assertFalse(isMacCompatibleApiBaseUrl(" https://api.example.test/"))
        assertFalse(isMacCompatibleApiBaseUrl("file:///tmp/api"))
        assertTrue(isMacCompatibleApiBaseUrl("https://api.example.test/"))
        assertTrue(isMacCompatibleApiBaseUrl("http://localhost:8000/"))
        assertTrue(isMacCompatibleApiBaseUrl("http://127.0.0.1:8000/"))
        assertTrue(isMacCompatibleApiBaseUrl("http://[::1]:8000/"))
        assertTrue(isMacCompatibleApiBaseUrl("https://api.example.test:65535/"))
    }

    @Test
    fun tokenActionsAndHookInstallRemainUsableInNarrowLargeTextLayout() {
        composeRule.setContent {
            Box(modifier = Modifier.width(240.dp)) {
                CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 1.5f)) {
                    ControlTowerTheme {
                        OneTimeTokenPanel(
                            token = "one-time-token-example",
                            provider = "ANTIGRAVITY",
                            agentName = "Antigravity connector",
                            agentId = "agent-example",
                            projectId = "project-example",
                            apiBaseUrl = "https://api.example.test/",
                            onCopy = {},
                            onCopyKeychainCommand = { true },
                            onCopyHookInstallCommand = { true },
                            onHide = {}
                        )
                    }
                }
            }
        }
        composeRule.onNodeWithText("Absolute local project directory")
            .performTextInput("/Users/test/Projects/example")

        val copyAction = composeRule.onNodeWithText("Copy sensitive token")
        val keychainAction = composeRule.onNodeWithText("Copy Keychain command")
        val hookAction = composeRule.onNodeWithText("Copy hook install command")
        val hideAction = composeRule.onNodeWithText("Hide now")
        copyAction.assertExists()
        keychainAction.assertExists()
        hookAction.assertExists()
        hideAction.assertExists()
        composeRule.runOnIdle {
            assertTrue(
                "Copy action should remain within the narrow viewport",
                copyAction.fetchSemanticsNode().boundsInRoot.right <= 240f
            )
            assertTrue(
                "Keychain copy action should remain within the narrow viewport",
                keychainAction.fetchSemanticsNode().boundsInRoot.right <= 240f
            )
            assertTrue(
                "Hook install copy action should remain within the narrow viewport",
                hookAction.fetchSemanticsNode().boundsInRoot.right <= 240f
            )
            assertTrue(
                "Hide action should remain within the narrow viewport",
                hideAction.fetchSemanticsNode().boundsInRoot.right <= 240f
            )
        }
    }

    @Test
    fun copyActionsKeepTokenSeparateAndConfirmMacCommands() {
        var sensitiveTokenCopied = false
        var keychainCommandCopied: String? = null
        var hookCommandCopied: String? = null
        composeRule.setContent {
            ControlTowerTheme {
                OneTimeTokenPanel(
                    token = "secret-example",
                    provider = "ANTIGRAVITY",
                    agentName = "Antigravity connector",
                    agentId = "agent-example",
                    projectId = "project-example",
                    apiBaseUrl = "https://api.example.test/",
                    onCopy = { sensitiveTokenCopied = true },
                    onCopyKeychainCommand = { command ->
                        keychainCommandCopied = command
                        true
                    },
                    onCopyHookInstallCommand = { command ->
                        hookCommandCopied = command
                        true
                    },
                    onHide = {}
                )
            }
        }

        composeRule.onNodeWithText("Copy Keychain command").performClick()
        composeRule.onNodeWithText(
            "Keychain command copied. Enter the token only at the secure prompt."
        ).assertExists()
        assertEquals(
            "security add-generic-password -U -s com.aixion.controltower.agent -a 'agent-example' -w",
            keychainCommandCopied
        )
        assertFalse(sensitiveTokenCopied)

        composeRule.onNodeWithText("Absolute local project directory")
            .performTextInput("/Users/test/Projects/example")
        composeRule.onNodeWithText("Copy hook install command").performClick()
        composeRule.onNodeWithText(
            "Hook install command copied. Run it from the Control Tower repository root."
        ).assertExists()
        assertNotNull(hookCommandCopied)
        assertFalse(sensitiveTokenCopied)
        assertFalse(keychainCommandCopied?.contains("secret-example") ?: false)
        assertFalse(hookCommandCopied?.contains("secret-example") ?: false)

        composeRule.onNodeWithText("Copy sensitive token").performClick()
        assertTrue(sensitiveTokenCopied)
    }
}
