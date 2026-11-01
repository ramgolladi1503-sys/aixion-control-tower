package com.aixion.controltower.notifications

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class ApprovalNotificationPreviewTest {
    @Test
    fun remoteTaskAndProjectDetailsNeverAppearInPreview() {
        val privateTitle = "Approval needed: Refactor customer billing service"
        val privateBody = "CRITICAL risk request mentions customer-account secrets."

        val preview = ApprovalNotificationPreview.fromUntrustedPush(
            remoteTitle = privateTitle,
            remoteBody = privateBody
        )

        assertEquals("Aixion Control Tower", preview.title)
        assertEquals("Approval needed. Open the app to review.", preview.body)
        assertFalse(preview.title.contains("customer", ignoreCase = true))
        assertFalse(preview.body.contains("secret", ignoreCase = true))
    }

    @Test
    fun emptyPushCopyStillProducesActionableGenericPreview() {
        val preview = ApprovalNotificationPreview.fromUntrustedPush(remoteTitle = null, remoteBody = "")

        assertEquals("Aixion Control Tower", preview.title)
        assertEquals("Approval needed. Open the app to review.", preview.body)
    }
}
