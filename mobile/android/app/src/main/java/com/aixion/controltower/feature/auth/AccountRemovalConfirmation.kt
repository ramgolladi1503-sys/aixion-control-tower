package com.aixion.controltower.feature.auth

import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable

@Composable
internal fun AccountRemovalConfirmation(
    loading: Boolean,
    onDismiss: () -> Unit,
    onConfirm: () -> Unit
) {
    AlertDialog(
        onDismissRequest = { if (!loading) onDismiss() },
        title = { Text("Confirm account removal request?") },
        text = {
            Text(
                "This request disables account access and revokes active sessions. " +
                    "Final deletion or anonymization is processed separately; some audit and security records may be retained."
            )
        },
        confirmButton = {
            TextButton(onClick = onConfirm, enabled = !loading) {
                Text(if (loading) "Submitting…" else "Submit request")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss, enabled = !loading) { Text("Cancel") }
        }
    )
}
