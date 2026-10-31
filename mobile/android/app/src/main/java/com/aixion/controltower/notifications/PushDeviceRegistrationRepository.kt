package com.aixion.controltower.notifications

import android.Manifest
import android.content.Context
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.ContextCompat
import com.aixion.controltower.BuildConfig
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.DeviceRegistrationDto
import com.aixion.controltower.core.api.dto.DeviceRegistrationRequestDto
import com.aixion.controltower.core.auth.SecureStringPreferences
import com.google.firebase.messaging.FirebaseMessaging
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.tasks.await
import kotlinx.coroutines.withTimeout
import retrofit2.HttpException

internal enum class PushRegistrationResult {
    REGISTERED,
    REGISTERED_CLEANUP_PENDING,
    PERMISSION_REQUIRED,
    PERMISSION_REQUIRED_CLEANUP_PENDING,
    AUTHENTICATION_REQUIRED,
    FIREBASE_NOT_CONFIGURED,
    BACKEND_UNAVAILABLE
}

internal interface PushRegistrationApi {
    suspend fun currentUserId(): String
    suspend fun registerDevice(payload: DeviceRegistrationRequestDto): DeviceRegistrationDto
    suspend fun unregisterDevice(deviceId: String)
}

internal interface PushRegistrationStateStore {
    fun migrateLegacyPendingToken()
    fun activeDeviceId(): String?
    fun activeUserId(): String?
    fun saveActiveRegistration(deviceId: String, userId: String)
    fun readPendingUnregistrations(): MutableSet<Pair<String, String>>
    fun writePendingUnregistrations(pending: Set<Pair<String, String>>)
    fun clearActiveRegistration()
    fun clearPendingFcmToken()
}

internal class PushDeviceRegistrationRepository internal constructor(
    private val api: PushRegistrationApi,
    private val hasNotificationPermission: () -> Boolean,
    private val currentFcmToken: suspend () -> String?,
    private val state: PushRegistrationStateStore,
    private val appVersion: String,
    private val firebaseConfigured: () -> Boolean = { BuildConfig.AIXION_FIREBASE_CONFIG_ENABLED }
) {
    constructor(context: Context) : this(
        api = ApiPushRegistrationAdapter(ApiClient.create(context.applicationContext)),
        hasNotificationPermission = {
            Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
                ContextCompat.checkSelfPermission(
                    context.applicationContext,
                    Manifest.permission.POST_NOTIFICATIONS
                ) == PackageManager.PERMISSION_GRANTED
        },
        currentFcmToken = {
            FirebaseMessaging.getInstance().token.await().takeIf(String::isNotBlank)
        },
        state = SharedPreferencesPushRegistrationStateStore(context.applicationContext),
        appVersion = BuildConfig.VERSION_NAME
    )

    suspend fun registerCurrentDevice(): PushRegistrationResult {
        if (!hasNotificationPermission()) {
            return unregisterCurrentDeviceResult()
        }

        // Migrate old plaintext tokens before network access. The FCM token is
        // never copied into registration state or logged by this repository.
        state.migrateLegacyPendingToken()

        val userId = try {
            api.currentUserId()
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: RegistrationAuthenticationRequiredException) {
            // Keep the pending cleanup record and stop background retries until
            // the authenticated session is repaired.
            return PushRegistrationResult.AUTHENTICATION_REQUIRED
        } catch (error: HttpException) {
            return classifyBackendFailure(error)
        } catch (_: Exception) {
            return PushRegistrationResult.BACKEND_UNAVAILABLE
        }
        if (retryPendingUnregistrationsForUser(userId)) {
            return PushRegistrationResult.AUTHENTICATION_REQUIRED
        }
        if (!firebaseConfigured()) {
            return PushRegistrationResult.FIREBASE_NOT_CONFIGURED
        }

        val token = try {
            currentFcmToken()?.takeIf(String::isNotBlank)
                ?: return PushRegistrationResult.BACKEND_UNAVAILABLE
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            return PushRegistrationResult.BACKEND_UNAVAILABLE
        }

        return try {
            val previousDeviceId = state.activeDeviceId()
            val previousUserId = state.activeUserId()
            val registration = api.registerDevice(
                DeviceRegistrationRequestDto(
                    token = token,
                    app_version = appVersion
                )
            )
            if (registration.id.isBlank() ||
                (!registration.user_id.isNullOrBlank() && registration.user_id != userId)
            ) {
                // Do not persist a response that cannot be bound to this device
                // and authenticated account. Keep prior local state and token
                // cleanup intent so the caller can retry after server recovery.
                return PushRegistrationResult.BACKEND_UNAVAILABLE
            }
            state.saveActiveRegistration(
                registration.id,
                registration.user_id?.takeIf(String::isNotBlank) ?: userId
            )
            state.clearPendingFcmToken()

            val pending = state.readPendingUnregistrations()
            if (previousDeviceId != null && previousDeviceId != registration.id &&
                !previousUserId.isNullOrBlank()
            ) {
                pending += previousDeviceId to previousUserId
            }
            // A successful registration may reassign this token to the active
            // account; cleanup for that same row would now be stale.
            pending.removeAll { it.first == registration.id }
            val cleanupPending = cleanupUnregistrations(pending, userId)
            if (cleanupPending.isNotEmpty()) {
                state.writePendingUnregistrations(cleanupPending)
                PushRegistrationResult.REGISTERED_CLEANUP_PENDING
            } else {
                state.writePendingUnregistrations(emptySet())
                PushRegistrationResult.REGISTERED
            }
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: RegistrationAuthenticationRequiredException) {
            PushRegistrationResult.AUTHENTICATION_REQUIRED
        } catch (error: HttpException) {
            classifyBackendFailure(error)
        } catch (_: Exception) {
            PushRegistrationResult.BACKEND_UNAVAILABLE
        }
    }

    suspend fun unregisterCurrentDevice(): Boolean {
        return unregisterCurrentDeviceResult() == PushRegistrationResult.PERMISSION_REQUIRED
    }

    private suspend fun unregisterCurrentDeviceResult(): PushRegistrationResult {
        val pending = state.readPendingUnregistrations()
        val deviceId = state.activeDeviceId()
        val deviceOwnerId = state.activeUserId()
        if (!deviceId.isNullOrBlank() && !deviceOwnerId.isNullOrBlank()) {
            pending += deviceId to deviceOwnerId
        }
        if (pending.isEmpty()) {
            state.clearActiveRegistration()
            return PushRegistrationResult.PERMISSION_REQUIRED
        }

        // Persist cleanup intent before network I/O so process death cannot
        // silently lose the only reference to the server registration.
        state.writePendingUnregistrations(pending)
        return try {
            val userId = api.currentUserId()
            val remaining = cleanupUnregistrations(pending, userId)
            state.writePendingUnregistrations(remaining)
            if (remaining.isEmpty()) {
                PushRegistrationResult.PERMISSION_REQUIRED
            } else {
                PushRegistrationResult.PERMISSION_REQUIRED_CLEANUP_PENDING
            }
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: RegistrationAuthenticationRequiredException) {
            // The cleanup intent is already durable; let session recovery retry it.
            PushRegistrationResult.AUTHENTICATION_REQUIRED
        } catch (error: HttpException) {
            val failure = classifyBackendFailure(error)
            if (failure == PushRegistrationResult.AUTHENTICATION_REQUIRED) {
                // The cleanup intent was saved before network I/O; leave it
                // durable and let login/session recovery retry it later.
                failure
            } else {
                PushRegistrationResult.PERMISSION_REQUIRED_CLEANUP_PENDING
            }
        } catch (_: Exception) {
            PushRegistrationResult.PERMISSION_REQUIRED_CLEANUP_PENDING
        } finally {
            state.clearActiveRegistration()
        }
    }

    suspend fun retryPendingUnregistrations(userId: String? = null) {
        val currentUserId = userId ?: try {
            api.currentUserId()
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            return
        }
        retryPendingUnregistrationsForUser(currentUserId)
    }

    /** Returns true only for auth rejection; transient per-device failures remain pending. */
    private suspend fun retryPendingUnregistrationsForUser(userId: String): Boolean {
        return try {
            state.writePendingUnregistrations(
                cleanupUnregistrations(state.readPendingUnregistrations(), userId)
            )
            false
        } catch (_: RegistrationAuthenticationRequiredException) {
            // Keep the persisted cleanup set unchanged; login/session recovery retries it.
            true
        }
    }

    private suspend fun cleanupUnregistrations(
        pending: Set<Pair<String, String>>,
        currentUserId: String
    ): Set<Pair<String, String>> {
        val remaining = pending.toMutableSet()
        for ((deviceId, ownerId) in pending) {
            if (ownerId != currentUserId) continue
            val removed = try {
                withTimeout(UNREGISTER_TIMEOUT_MILLIS) {
                    api.unregisterDevice(deviceId)
                }
                true
            } catch (cancelled: CancellationException) {
                if (cancelled !is kotlinx.coroutines.TimeoutCancellationException) throw cancelled
                false
            } catch (error: HttpException) {
                if (error.code() == 401 || error.code() == 403) {
                    throw RegistrationAuthenticationRequiredException()
                }
                false
            } catch (_: Exception) {
                false
            }
            if (removed) remaining.remove(deviceId to ownerId)
        }
        return remaining
    }

    private fun classifyBackendFailure(error: HttpException): PushRegistrationResult =
        if (error.code() == 401 || error.code() == 403) {
            PushRegistrationResult.AUTHENTICATION_REQUIRED
        } else {
            PushRegistrationResult.BACKEND_UNAVAILABLE
        }

    private class RegistrationAuthenticationRequiredException : Exception()

    private class ApiPushRegistrationAdapter(
        private val api: ControlTowerApi
    ) : PushRegistrationApi {
        override suspend fun currentUserId(): String = api.me().id

        override suspend fun registerDevice(
            payload: DeviceRegistrationRequestDto
        ): DeviceRegistrationDto = api.registerNotificationDevice(payload)

        override suspend fun unregisterDevice(deviceId: String) {
            api.unregisterNotificationDevice(deviceId)
        }
    }

    private class SharedPreferencesPushRegistrationStateStore(
        context: Context
    ) : PushRegistrationStateStore {
        private val appContext = context.applicationContext
        private val preferences = appContext.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

        override fun migrateLegacyPendingToken() {
            // Reading through the encrypted store migrates values written by
            // older releases before any request sends a token to the backend.
            SecureStringPreferences.get(
                appContext,
                PREFERENCES,
                PENDING_FCM_TOKEN,
                PUSH_TOKEN_KEY_ALIAS
            )
        }

        override fun activeDeviceId(): String? = preferences.getString(REGISTERED_DEVICE_ID, null)

        override fun activeUserId(): String? = preferences.getString(REGISTERED_USER_ID, null)

        override fun saveActiveRegistration(deviceId: String, userId: String) {
            preferences.edit()
                .putString(REGISTERED_DEVICE_ID, deviceId)
                .putString(REGISTERED_USER_ID, userId)
                .apply()
        }

        override fun readPendingUnregistrations(): MutableSet<Pair<String, String>> {
            return preferences.getStringSet(PENDING_UNREGISTER_DEVICE_IDS, emptySet())
                .orEmpty()
                .mapNotNull { encoded ->
                    val separator = encoded.indexOf('|')
                    if (separator <= 0 || separator == encoded.lastIndex) null
                    else encoded.substring(0, separator) to encoded.substring(separator + 1)
                }
                .toMutableSet()
        }

        override fun writePendingUnregistrations(pending: Set<Pair<String, String>>) {
            preferences.edit()
                .putStringSet(
                    PENDING_UNREGISTER_DEVICE_IDS,
                    pending.map { (id, owner) -> "$id|$owner" }.toSet()
                )
                .commit()
        }

        override fun clearActiveRegistration() {
            preferences.edit()
                .remove(REGISTERED_DEVICE_ID)
                .remove(REGISTERED_USER_ID)
                .commit()
            clearPendingFcmToken()
        }

        override fun clearPendingFcmToken() {
            SecureStringPreferences.remove(
                appContext,
                PREFERENCES,
                PENDING_FCM_TOKEN,
                PUSH_TOKEN_KEY_ALIAS
            )
        }

        private companion object {
            const val PREFERENCES = "aixion_push"
            const val REGISTERED_DEVICE_ID = "registered_device_id"
            const val REGISTERED_USER_ID = "registered_user_id"
            const val PENDING_FCM_TOKEN = "pending_fcm_token"
            const val PUSH_TOKEN_KEY_ALIAS = "com.aixion.controltower.push.token.v1"
            const val PENDING_UNREGISTER_DEVICE_IDS = "pending_unregister_device_ids"
        }
    }

    private companion object {
        const val UNREGISTER_TIMEOUT_MILLIS = 4_000L
    }
}
