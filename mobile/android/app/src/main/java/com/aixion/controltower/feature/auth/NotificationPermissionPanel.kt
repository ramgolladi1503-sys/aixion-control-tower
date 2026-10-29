package com.aixion.controltower.feature.auth

import android.Manifest
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import com.aixion.controltower.BuildConfig
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.notifications.PushDeviceRegistrationRepository
import com.aixion.controltower.notifications.PushRegistrationResult

internal enum class PushRegistrationUiState {
    CHECKING,
    REGISTERED,
    REGISTERED_CLEANUP_PENDING,
    PERMISSION_REQUIRED,
    PERMISSION_REQUIRED_CLEANUP_PENDING,
    FIREBASE_NOT_CONFIGURED,
    BACKEND_UNAVAILABLE
}

internal enum class NotificationPermissionState {
    GRANTED,
    DENIED,
    NOT_REQUIRED
}

internal data class NotificationPanelMessage(
    val headline: String,
    val supportingDetail: String?
)

internal object NotificationPanelCopy {
    fun message(
        permissionState: NotificationPermissionState,
        registrationState: PushRegistrationUiState
    ): NotificationPanelMessage {
        val permissionDetail = when (permissionState) {
            NotificationPermissionState.GRANTED -> "Android allows notifications on this device."
            NotificationPermissionState.DENIED -> "Android notifications are off, so alerts cannot appear until you allow them."
            NotificationPermissionState.NOT_REQUIRED -> "This Android version does not require a notification permission prompt."
        }
        return when (registrationState) {
            PushRegistrationUiState.CHECKING -> NotificationPanelMessage(
                headline = "Checking approval alert setup…",
                supportingDetail = permissionDetail
            )
            PushRegistrationUiState.REGISTERED -> if (permissionState == NotificationPermissionState.DENIED) {
                NotificationPanelMessage(
                    headline = "Android notifications are off.",
                    supportingDetail = "A server registration exists, but Android cannot display alerts until you allow notifications."
                )
            } else {
                NotificationPanelMessage(
                    headline = "Device registered with the server.",
                    supportingDetail = "$permissionDetail This confirms registration only, not delivery of a push alert."
                )
            }
            PushRegistrationUiState.REGISTERED_CLEANUP_PENDING -> if (permissionState == NotificationPermissionState.DENIED) {
                NotificationPanelMessage(
                    headline = "Android notifications are off.",
                    supportingDetail = "Allow notifications for approval alerts. Cleanup of an older device registration is pending and will retry when the owning account is available."
                )
            } else {
                NotificationPanelMessage(
                    headline = "Device registered with the server.",
                    supportingDetail = "An older device registration could not be removed yet. Cleanup will retry when the owning account is available. This does not confirm push delivery."
                )
            }
            PushRegistrationUiState.PERMISSION_REQUIRED_CLEANUP_PENDING -> NotificationPanelMessage(
                headline = "Android notifications are off.",
                supportingDetail = "Allow notifications for approval alerts. Server cleanup is pending and will retry when the owning account is available."
            )
            PushRegistrationUiState.FIREBASE_NOT_CONFIGURED -> NotificationPanelMessage(
                headline = "Push alerts are unavailable in this app build.",
                supportingDetail = buildString {
                    append("Firebase is not configured for this app build.")
                    if (permissionState == NotificationPermissionState.DENIED) {
                        append(" Android notifications are also off.")
                    }
                }
            )
            PushRegistrationUiState.BACKEND_UNAVAILABLE -> NotificationPanelMessage(
                headline = "This device could not be registered for approval alerts.",
                supportingDetail = buildString {
                    append("Server registration failed. Reopen Account to retry.")
                    if (permissionState == NotificationPermissionState.DENIED) {
                        append(" Android notifications are also off.")
                    }
                }
            )
            PushRegistrationUiState.PERMISSION_REQUIRED -> if (permissionState == NotificationPermissionState.DENIED) {
                NotificationPanelMessage(
                    headline = "Android notifications are off.",
                    supportingDetail = "Allow notifications to register this device for approval alerts."
                )
            } else {
                NotificationPanelMessage(
                    headline = "The server requires notification permission.",
                    supportingDetail = "Check Android notification settings, then reopen Account to retry registration."
                )
            }
        }
    }
}

@Composable
internal fun NotificationPermissionPanel() {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val requiresRuntimePermission = Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
    val initialState = remember {
        when {
            !requiresRuntimePermission -> NotificationPermissionState.NOT_REQUIRED
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED ->
                NotificationPermissionState.GRANTED
            else -> NotificationPermissionState.DENIED
        }
    }
    var permissionState by remember { mutableStateOf(initialState) }
    var registrationRefresh by remember { mutableStateOf(0) }
    var registrationState by remember {
        mutableStateOf(
            if (initialState == NotificationPermissionState.DENIED) {
                if (BuildConfig.AIXION_FIREBASE_CONFIG_ENABLED) {
                    PushRegistrationUiState.PERMISSION_REQUIRED
                } else {
                    PushRegistrationUiState.FIREBASE_NOT_CONFIGURED
                }
            } else {
                PushRegistrationUiState.CHECKING
            }
        )
    }
    val permissionLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.RequestPermission()
    ) { granted ->
        permissionState = if (granted) {
            NotificationPermissionState.GRANTED
        } else {
            NotificationPermissionState.DENIED
        }
    }

    LaunchedEffect(permissionState, registrationRefresh) {
        registrationState = PushRegistrationUiState.CHECKING
        val result = PushDeviceRegistrationRepository(context).registerCurrentDevice().toUiState()
        registrationState = if (
            !BuildConfig.AIXION_FIREBASE_CONFIG_ENABLED &&
            result == PushRegistrationUiState.PERMISSION_REQUIRED
        ) {
            PushRegistrationUiState.FIREBASE_NOT_CONFIGURED
        } else {
            result
        }
    }

    androidx.compose.runtime.DisposableEffect(lifecycleOwner, requiresRuntimePermission) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) {
                val notificationsAllowed = !requiresRuntimePermission ||
                    ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
                permissionState = if (notificationsAllowed) {
                    if (registrationState != PushRegistrationUiState.CHECKING) {
                        registrationRefresh += 1
                    }
                    if (requiresRuntimePermission) NotificationPermissionState.GRANTED else NotificationPermissionState.NOT_REQUIRED
                } else {
                    registrationRefresh += 1
                    NotificationPermissionState.DENIED
                }
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    TowerPanel(elevated = true) {
        NotificationStatusContent(permissionState, registrationState)
        if (shouldShowNotificationPermissionAction(
                permissionState = permissionState,
                registrationState = registrationState,
                firebaseConfigured = BuildConfig.AIXION_FIREBASE_CONFIG_ENABLED
            )
        ) {
            Spacer(modifier = Modifier.height(8.dp))
            Button(
                onClick = {
                    if (requiresRuntimePermission) {
                        permissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
                    }
                },
                modifier = Modifier.fillMaxWidth()
            ) {
                Text("Enable approval alerts")
            }
        }
    }
}

@Composable
internal fun NotificationStatusContent(
    permissionState: NotificationPermissionState,
    registrationState: PushRegistrationUiState
) {
    val palette = LocalTowerPalette.current
    val message = NotificationPanelCopy.message(permissionState, registrationState)
    Text(
        text = "Approval notifications",
        modifier = Modifier.semantics { heading() },
        color = palette.ink,
        style = MaterialTheme.typography.titleMedium
    )
    Spacer(modifier = Modifier.height(8.dp))
    Text(
        text = message.headline,
        color = palette.ink,
        style = MaterialTheme.typography.titleSmall,
        modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite }
    )
    message.supportingDetail?.let { detail ->
        Text(text = detail, color = palette.inkMuted, style = MaterialTheme.typography.bodyMedium)
    }
}

internal fun shouldShowNotificationPermissionAction(
    permissionState: NotificationPermissionState,
    registrationState: PushRegistrationUiState,
    firebaseConfigured: Boolean
): Boolean = permissionState == NotificationPermissionState.DENIED &&
    firebaseConfigured &&
    registrationState == PushRegistrationUiState.PERMISSION_REQUIRED

private fun PushRegistrationResult.toUiState(): PushRegistrationUiState = when (this) {
    PushRegistrationResult.REGISTERED -> PushRegistrationUiState.REGISTERED
    PushRegistrationResult.REGISTERED_CLEANUP_PENDING -> PushRegistrationUiState.REGISTERED_CLEANUP_PENDING
    PushRegistrationResult.PERMISSION_REQUIRED -> PushRegistrationUiState.PERMISSION_REQUIRED
    PushRegistrationResult.PERMISSION_REQUIRED_CLEANUP_PENDING ->
        PushRegistrationUiState.PERMISSION_REQUIRED_CLEANUP_PENDING
    PushRegistrationResult.FIREBASE_NOT_CONFIGURED -> PushRegistrationUiState.FIREBASE_NOT_CONFIGURED
    PushRegistrationResult.AUTHENTICATION_REQUIRED -> PushRegistrationUiState.BACKEND_UNAVAILABLE
    PushRegistrationResult.BACKEND_UNAVAILABLE -> PushRegistrationUiState.BACKEND_UNAVAILABLE
}
