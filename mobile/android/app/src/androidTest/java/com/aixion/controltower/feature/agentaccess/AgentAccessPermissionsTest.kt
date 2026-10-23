package com.aixion.controltower.feature.agentaccess

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.graphics.toPixelMap
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import com.aixion.controltower.core.ui.theme.DarkTowerPalette
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class AgentAccessPermissionsTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun connectorScopeAndDecisionLimitationsRemainVisibleAsReadableContent() {
        composeRule.setContent {
            ControlTowerTheme {
                ConnectorPermissionsDisclosure()
            }
        }

        composeRule.onNodeWithText("Granted actions").assertExists()
        composeRule.onNodeWithText(
            "This connector is limited to the selected project and these five approval-only actions:"
        ).assertExists()

        listOf(
            "CREATE_APPROVAL",
            "READ_APPROVAL_DECISION",
            "CONSUME_APPROVAL_DECISION",
            "REPORT_APPROVAL_DELIVERY_FAILURE",
            "EXPIRE_OWN_APPROVAL"
        ).forEach { action ->
            composeRule.onNodeWithText("• $action").assertExists()
        }

        composeRule.onNodeWithText(
            "Consume only returns an already-approved decision once. This credential cannot approve requests or start tools; the paused host agent continues only after that decision is returned."
        ).assertTextEquals(
            "Consume only returns an already-approved decision once. This credential cannot approve requests or start tools; the paused host agent continues only after that decision is returned."
        )
    }

    @Test
    fun connectorSetupIsNeverPresentedAsAConfirmedLiveConnection() {
        composeRule.setContent {
            ControlTowerTheme {
                AgentAccessStatusBadge(isLoading = false)
            }
        }

        composeRule.onNodeWithText("Connector setup").assertExists()
        composeRule.onNodeWithText("MAC AGENT ACCESS").assertDoesNotExist()
    }

    @Test
    fun connectorStatusShowsSyncingWhileLoading() {
        composeRule.setContent {
            ControlTowerTheme {
                AgentAccessStatusBadge(isLoading = true)
            }
        }

        composeRule.onNodeWithText("Syncing").assertExists()
        composeRule.onNodeWithText("Connector setup").assertDoesNotExist()
    }

    @Test
    fun connectorPermissionsUseDarkPaletteTextColors() {
        composeRule.setContent {
            ControlTowerTheme {
                CompositionLocalProvider(LocalTowerPalette provides DarkTowerPalette) {
                    Box(
                        modifier = Modifier
                            .fillMaxSize()
                            .background(DarkTowerPalette.canvas)
                            .padding(16.dp)
                    ) {
                        TowerPanel(elevated = true) {
                            ConnectorPermissionsDisclosure()
                        }
                    }
                }
            }
        }

        assertTextContainsColor("Granted actions", DarkTowerPalette.ink)
        assertTextContainsColor(
            "This connector is limited to the selected project and these five approval-only actions:",
            DarkTowerPalette.inkMuted
        )
    }

    private fun assertTextContainsColor(label: String, expectedColor: Color) {
        val bounds = composeRule.onNodeWithText(label).fetchSemanticsNode().boundsInRoot
        val image = composeRule.onRoot().captureToImage().toPixelMap()
        val left = bounds.left.toInt().coerceIn(0, image.width - 1)
        val right = bounds.right.toInt().coerceIn(0, image.width - 1)
        val top = bounds.top.toInt().coerceIn(0, image.height - 1)
        val bottom = bounds.bottom.toInt().coerceIn(0, image.height - 1)

        val matchingPixelExists = (top..bottom).any { y ->
            (left..right).any { x -> image[x, y].toArgb() == expectedColor.toArgb() }
        }
        assertTrue("$label should render with theme color $expectedColor", matchingPixelExists)
    }
}
