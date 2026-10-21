package com.aixion.controltower.notifications

import android.content.Context
import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import com.aixion.controltower.BuildConfig
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.api.dto.DeviceRegistrationRequestDto
import com.aixion.controltower.core.auth.SecureStringPreferences
import com.google.firebase.messaging.FirebaseMessaging
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.tasks.await
import kotlinx.coroutines.withTimeout
import androidx.core.content.ContextCompat

internal enum class PushRegistrationResult {
    REGISTERED,
    REGISTERED_CLEANUP_PENDING,
    PERMISSION_REQUIRED,
    FIREBASE_NOT_CONFIGURED,
    BACKEND_UNAVAILABLE
}

internal class PushDeviceRegistrationRepository(context: Context) {
    private val appContext = context.applicationContext
    private val api = ApiClient.create(appContext)
    private val preferences = appContext.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    suspend fun registerCurrentDevice(): PushRegistrationResult {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(appContext, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            return PushRegistrationResult.PERMISSION_REQUIRED
        }

        // An older app version stored FCM refresh tokens as plaintext. Reading
        // through the encrypted store migrates that value before any network call.
        SecureStringPreferences.get(
            appContext,
            PREFERENCES,
            PENDING_FCM_TOKEN,
            PUSH_TOKEN_KEY_ALIAS
        )

        val userId = try {
            api.me().id
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            return PushRegistrationResult.BACKEND_UNAVAILABLE
        }
        retryPendingUnregistrations(userId)

        val token = try {
            FirebaseMessaging.getInstance().token.await().takeIf(String::isNotBlank)
                ?: return PushRegistrationResult.FIREBASE_NOT_CONFIGURED
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: IllegalStateException) {
            return PushRegistrationResult.FIREBASE_NOT_CONFIGURED
        } catch (_: Exception) {
            return PushRegistrationResult.FIREBASE_NOT_CONFIGURED
        }

        return try {
            val previousDeviceId = preferences.getString(REGISTERED_DEVICE_ID, null)
            val previousUserId = preferences.getString(REGISTERED_USER_ID, null)
            val registration = api.registerNotificationDevice(
                DeviceRegistrationRequestDto(
                    token = token,
                    app_version = BuildConfig.VERSION_NAME
                )
            )
            preferences.edit()
                .putString(REGISTERED_DEVICE_ID, registration.id)
                .putString(REGISTERED_USER_ID, registration.user_id ?: userId)
                .apply()
            clearPendingFcmToken()

            val pending = readPendingUnregistrations()
            if (previousDeviceId != null && previousDeviceId != registration.id && !previousUserId.isNullOrBlank()) {
                pending += previousDeviceId to previousUserId
            }
            // The server binds one FCM token to one account. A successful
            // register may have reassigned this installation after an account
            // switch, making any pending cleanup reference to this same row stale.
            pending.removeAll { it.first == registration.id }
            val cleanupPending = cleanupUnregistrations(pending, userId)
            if (cleanupPending.isNotEmpty()) {
                writePendingUnregistrations(cleanupPending)
                PushRegistrationResult.REGISTERED_CLEANUP_PENDING
            } else {
                writePendingUnregistrations(emptySet())
                PushRegistrationResult.REGISTERED
            }
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            PushRegistrationResult.BACKEND_UNAVAILABLE
        }
    }

    suspend fun unregisterCurrentDevice(): Boolean {
        val pending = readPendingUnregistrations()
        val deviceId = preferences.getString(REGISTERED_DEVICE_ID, null)
        val deviceOwnerId = preferences.getString(REGISTERED_USER_ID, null)
        if (!deviceId.isNullOrBlank() && !deviceOwnerId.isNullOrBlank()) {
            pending += deviceId to deviceOwnerId
        }
        if (pending.isEmpty()) {
            clearActiveRegistration()
            return true
        }

        // Persist the cleanup intent before attempting network I/O so a process
        // death cannot silently lose the only reference to the server record.
        writePendingUnregistrations(pending)
        return try {
            val userId = api.me().id
            val remaining = cleanupUnregistrations(pending, userId)
            writePendingUnregistrations(remaining)
            pending.none { it.second == userId && it in remaining }
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            false
        } finally {
            clearActiveRegistration()
        }
    }

    suspend fun retryPendingUnregistrations(userId: String? = null) {
        val currentUserId = userId ?: try {
            api.me().id
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            return
        }
        writePendingUnregistrations(cleanupUnregistrations(readPendingUnregistrations(), currentUserId))
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
                    api.unregisterNotificationDevice(deviceId)
                }
                true
            } catch (cancelled: CancellationException) {
                if (cancelled !is kotlinx.coroutines.TimeoutCancellationException) throw cancelled
                false
            } catch (_: Exception) {
                false
            }
            if (removed) remaining.remove(deviceId to ownerId)
        }
        return remaining
    }

    private fun readPendingUnregistrations(): MutableSet<Pair<String, String>> {
        return preferences.getStringSet(PENDING_UNREGISTER_DEVICE_IDS, emptySet())
            .orEmpty()
            .mapNotNull { encoded ->
                val separator = encoded.indexOf('|')
                if (separator <= 0 || separator == encoded.lastIndex) null
                else encoded.substring(0, separator) to encoded.substring(separator + 1)
            }
            .toMutableSet()
    }

    private fun writePendingUnregistrations(pending: Set<Pair<String, String>>) {
        preferences.edit()
            .putStringSet(PENDING_UNREGISTER_DEVICE_IDS, pending.map { (id, owner) -> "$id|$owner" }.toSet())
            .commit()
    }

    private fun clearActiveRegistration() {
        preferences.edit()
            .remove(REGISTERED_DEVICE_ID)
            .remove(REGISTERED_USER_ID)
            .commit()
        clearPendingFcmToken()
    }

    private fun clearPendingFcmToken() {
        SecureStringPreferences.remove(
            appContext,
            PREFERENCES,
            PENDING_FCM_TOKEN,
            PUSH_TOKEN_KEY_ALIAS
        )
    }

    companion object {
        private const val PREFERENCES = "aixion_push"
        private const val REGISTERED_DEVICE_ID = "registered_device_id"
        private const val REGISTERED_USER_ID = "registered_user_id"
        private const val PENDING_FCM_TOKEN = "pending_fcm_token"
        private const val PUSH_TOKEN_KEY_ALIAS = "com.aixion.controltower.push.token.v1"
        private const val PENDING_UNREGISTER_DEVICE_IDS = "pending_unregister_device_ids"
        private const val UNREGISTER_TIMEOUT_MILLIS = 4_000L
    }
}
