package com.aixion.controltower.feature.auth

import com.aixion.controltower.core.ui.theme.DarkTowerPalette
import com.aixion.controltower.core.ui.theme.LightTowerPalette
import org.junit.Assert.assertEquals
import org.junit.Test

class AuthFeedbackMessageColorTest {
    @Test
    fun informationUsesDedicatedInfoTokenInBothPalettes() {
        assertEquals(LightTowerPalette.info, authFeedbackColor(AuthMessageSeverity.INFO, LightTowerPalette))
        assertEquals(DarkTowerPalette.info, authFeedbackColor(AuthMessageSeverity.INFO, DarkTowerPalette))
    }

    @Test
    fun warningAndErrorKeepTheirSemanticTokensInBothPalettes() {
        assertEquals(LightTowerPalette.caution, authFeedbackColor(AuthMessageSeverity.WARNING, LightTowerPalette))
        assertEquals(DarkTowerPalette.caution, authFeedbackColor(AuthMessageSeverity.WARNING, DarkTowerPalette))
        assertEquals(LightTowerPalette.danger, authFeedbackColor(AuthMessageSeverity.ERROR, LightTowerPalette))
        assertEquals(DarkTowerPalette.danger, authFeedbackColor(AuthMessageSeverity.ERROR, DarkTowerPalette))
    }
}
