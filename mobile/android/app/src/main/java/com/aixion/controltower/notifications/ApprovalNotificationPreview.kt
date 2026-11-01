package com.aixion.controltower.notifications

/** Minimal lock-screen copy for approval pushes; remote text may contain private work details. */
internal data class ApprovalNotificationPreview(
    val title: String,
    val body: String
) {
    companion object {
        /** Remote push title/body are deliberately ignored until an explicit privacy opt-in exists. */
        fun fromUntrustedPush(
            @Suppress("UNUSED_PARAMETER") remoteTitle: String?,
            @Suppress("UNUSED_PARAMETER") remoteBody: String?
        ) = ApprovalNotificationPreview(
            title = "Aixion Control Tower",
            body = "Approval needed. Open the app to review."
        )
    }
}
