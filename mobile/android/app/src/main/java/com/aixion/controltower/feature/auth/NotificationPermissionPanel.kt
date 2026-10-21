package com.aixion.controltower.feature.auth

import android.Manifest
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
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary
import com.aixion.controltower.notifications.PushDeviceRegistrationRepository
import com.aixion.controltower.notifications.PushRegistrationResult

internal enum class PushRegistrationUiState {
    CHECKING,
    REGISTERED,
    REGISTERED_CLEANUP_PENDING,
    PERMISSION_REQUIRED,
    FIREBASE_NOT_CONFIGURED,
    BACKEND_UNAVAILABLE
}

internal object PushRegistrationCopy {
    fun status(state: PushRegistrationUiState): String = when (state) {
        PushRegistrationUiState.CHECKING -> "Connecting this device to approval alerts…"
        PushRegistrationUiState.REGISTERED -> "This device is registered to receive approval alerts."
        PushRegistrationUiState.REGISTERED_CLEANUP_PENDING -> "Alerts are registered, but an older device registration could not be removed yet."
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
        NotificationPermissionState.GRANTED -> "Approval alerts are enabled on this device."
        NotificationPermissionState.DENIED -> "Approval alerts are off. Android will not show push notifications until you allow them."
        NotificationPermissionState.NOT_REQUIRED -> "This Android version does not require a notification permission prompt."
    }

    fun badge(state: NotificationPermissionState): String = when (state) {
        NotificationPermissionState.GRANTED -> "ALERTS ENABLED"
        NotificationPermissionState.DENIED -> "ALERTS OFF"
        NotificationPermissionState.NOT_REQUIRED -> "SYSTEM MANAGED"
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
                PushRegistrationUiState.PERMISSION_REQUIRED
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
        if (permissionState == NotificationPermissionState.DENIED) {
            registrationState = PushRegistrationUiState.PERMISSION_REQUIRED
            return@LaunchedEffect
        }
        registrationState = PushRegistrationUiState.CHECKING
        registrationState = PushDeviceRegistrationRepository(context).registerCurrentDevice().toUiState()
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
            color = if (permissionState == NotificationPermissionState.GRANTED) RiskLow else RiskMedium
        )
        Spacer(modifier = Modifier.height(8.dp))
        Text("Approval notifications", color = TowerTextPrimary)
        Text(NotificationPermissionCopy.status(permissionState), color = TowerTextMuted)
        Text(PushRegistrationCopy.status(registrationState), color = TowerTextMuted)
        if (permissionState == NotificationPermissionState.DENIED) {
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

private fun PushRegistrationResult.toUiState(): PushRegistrationUiState = when (this) {
    PushRegistrationResult.REGISTERED -> PushRegistrationUiState.REGISTERED
    PushRegistrationResult.REGISTERED_CLEANUP_PENDING -> PushRegistrationUiState.REGISTERED_CLEANUP_PENDING
    PushRegistrationResult.PERMISSION_REQUIRED -> PushRegistrationUiState.PERMISSION_REQUIRED
    PushRegistrationResult.FIREBASE_NOT_CONFIGURED -> PushRegistrationUiState.FIREBASE_NOT_CONFIGURED
    PushRegistrationResult.BACKEND_UNAVAILABLE -> PushRegistrationUiState.BACKEND_UNAVAILABLE
}
