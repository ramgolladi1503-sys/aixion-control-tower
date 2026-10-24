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
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import com.aixion.controltower.BuildConfig
import com.aixion.controltower.core.ui.components.StatusBadge
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

internal object PushRegistrationCopy {
    fun status(state: PushRegistrationUiState): String = when (state) {
        PushRegistrationUiState.CHECKING -> "Connecting this device to approval alerts…"
        PushRegistrationUiState.REGISTERED -> "This device is registered to receive approval alerts."
        PushRegistrationUiState.REGISTERED_CLEANUP_PENDING -> "Alerts are registered, but an older device registration could not be removed yet."
        PushRegistrationUiState.PERMISSION_REQUIRED_CLEANUP_PENDING -> "Notifications are off, and server cleanup is pending. Cleanup will retry when the owning account is available."
        PushRegistrationUiState.FIREBASE_NOT_CONFIGURED -> "Firebase is not configured for this app build. Push alerts are unavailable."
        PushRegistrationUiState.BACKEND_UNAVAILABLE -> "Could not register this device with the server. Reopen Account to retry."
        PushRegistrationUiState.PERMISSION_REQUIRED -> "Allow notifications to register this device for approval alerts."
    }
}

internal enum class NotificationPermissionState {
    GRANTED,
    DENIED,
    NOT_REQUIRED
}

internal object NotificationPermissionCopy {
    fun status(state: NotificationPermissionState): String = when (state) {
        NotificationPermissionState.GRANTED -> "Android allows notifications on this device."
        NotificationPermissionState.DENIED -> "Android notifications are off. Push notifications cannot appear until you allow them."
        NotificationPermissionState.NOT_REQUIRED -> "This Android version does not require a notification permission prompt."
    }

    fun badge(state: NotificationPermissionState): String = when (state) {
        NotificationPermissionState.GRANTED -> "Permission allowed"
        NotificationPermissionState.DENIED -> "Permission off"
        NotificationPermissionState.NOT_REQUIRED -> "System managed"
    }
}

@Composable
internal fun NotificationPermissionPanel() {
    val palette = LocalTowerPalette.current
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
        StatusBadge(
            label = NotificationPermissionCopy.badge(permissionState),
            color = if (permissionState == NotificationPermissionState.GRANTED) palette.positive else palette.caution,
            uppercase = false
        )
        Spacer(modifier = Modifier.height(8.dp))
        Text("Approval notifications", color = palette.ink)
        NotificationStatusAnnouncement(
            text = NotificationPermissionCopy.status(permissionState),
            color = palette.inkMuted
        )
        NotificationStatusAnnouncement(
            text = PushRegistrationCopy.status(registrationState),
            color = palette.inkMuted
        )
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
internal fun NotificationStatusAnnouncement(text: String, color: androidx.compose.ui.graphics.Color) {
    Text(
        text = text,
        color = color,
        modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite }
    )
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
    PushRegistrationResult.BACKEND_UNAVAILABLE -> PushRegistrationUiState.BACKEND_UNAVAILABLE
}
