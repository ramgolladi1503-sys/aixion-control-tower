package com.aixion.controltower.feature.auth

import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable

object LogoutConfirmationCopy {
    const val TITLE = "Log out of Control Tower?"
    const val MESSAGE = "You will need to sign in again to review approvals."
    const val CONFIRM = "Log out"
    const val DISMISS = "Stay signed in"
}

@Composable
fun LogoutConfirmationDialog(
    visible: Boolean,
    onConfirm: () -> Unit,
    onDismiss: () -> Unit
) {
    if (!visible) return

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(LogoutConfirmationCopy.TITLE) },
        text = { Text(LogoutConfirmationCopy.MESSAGE) },
        confirmButton = {
            TextButton(onClick = onConfirm) {
                Text(LogoutConfirmationCopy.CONFIRM)
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text(LogoutConfirmationCopy.DISMISS)
            }
        }
    )
}
