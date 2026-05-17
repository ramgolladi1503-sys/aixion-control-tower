package com.aixion.controltower.feature.auth

import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable

object LogoutConfirmationCopy {
    const val TITLE = "Do you wish to logout?"
    const val MESSAGE = "Your local session will be cleared and you will return to login."
    const val CONFIRM = "Yes"
    const val DISMISS = "No"
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
