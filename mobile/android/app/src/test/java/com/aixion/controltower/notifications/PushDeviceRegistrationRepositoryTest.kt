package com.aixion.controltower.notifications

import com.aixion.controltower.core.api.dto.DeviceRegistrationDto
import com.aixion.controltower.core.api.dto.DeviceRegistrationRequestDto
import java.io.IOException
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PushDeviceRegistrationRepositoryTest {
    @Test
    fun accountSwitchKeepsOldRegistrationUntilItsOwnerCanRemoveIt() = runTest {
        val state = FakeRegistrationState(
            deviceId = "device-old",
            userId = "user-old"
        )
        val api = FakeRegistrationApi(
            userId = "user-new",
            registration = DeviceRegistrationDto(id = "device-new", user_id = "user-new")
        )
        val repository = repository(
            api = api,
            state = state,
            token = { "fcm-token-new" }
        )

        val result = repository.registerCurrentDevice()

        assertEquals(PushRegistrationResult.REGISTERED_CLEANUP_PENDING, result)
        assertEquals("device-new" to "user-new", state.activeRegistration)
        assertEquals(setOf("device-old" to "user-old"), state.pendingUnregistrations)
        assertTrue(api.unregisteredDeviceIds.isEmpty())
        assertEquals(listOf("fcm-token-new"), api.registrationRequests.map { it.token })
    }

    @Test
    fun eachRegistrationUsesTheCurrentFcmTokenAfterTokenRefresh() = runTest {
        val state = FakeRegistrationState()
        val api = FakeRegistrationApi(
            userId = "user-current",
            registration = DeviceRegistrationDto(id = "device-current", user_id = "user-current")
        )
        val tokens = ArrayDeque(listOf("fcm-token-before-refresh", "fcm-token-after-refresh"))
        val repository = repository(api = api, state = state, token = { tokens.removeFirst() })

        assertEquals(PushRegistrationResult.REGISTERED, repository.registerCurrentDevice())
        assertEquals(PushRegistrationResult.REGISTERED, repository.registerCurrentDevice())

        assertEquals(
            listOf("fcm-token-before-refresh", "fcm-token-after-refresh"),
            api.registrationRequests.map { it.token }
        )
        assertEquals("device-current" to "user-current", state.activeRegistration)
    }

    @Test
    fun unauthenticatedRegistrationFailsClosedWithoutFetchingOrSendingToken() = runTest {
        val state = FakeRegistrationState()
        val api = FakeRegistrationApi(userId = "unused").apply {
            currentUserFailure = IOException("authentication required")
        }
        var tokenFetchCount = 0
        val repository = repository(
            api = api,
            state = state,
            token = {
                tokenFetchCount += 1
                "must-not-be-fetched"
            }
        )

        val result = repository.registerCurrentDevice()

        assertEquals(PushRegistrationResult.BACKEND_UNAVAILABLE, result)
        assertEquals(0, tokenFetchCount)
        assertTrue(api.registrationRequests.isEmpty())
        assertEquals(null, state.activeRegistration)
    }

    @Test
    fun deniedPermissionUnregistersOwnedDeviceWithoutFetchingOrSendingToken() = runTest {
        val state = FakeRegistrationState(
            deviceId = "device-current",
            userId = "user-current"
        )
        val api = FakeRegistrationApi(userId = "user-current")
        var tokenFetchCount = 0
        val repository = repository(
            api = api,
            state = state,
            hasPermission = false,
            token = {
                tokenFetchCount += 1
                "should-not-be-requested"
            }
        )

        val result = repository.registerCurrentDevice()

        assertEquals(PushRegistrationResult.PERMISSION_REQUIRED, result)
        assertEquals(listOf("device-current"), api.unregisteredDeviceIds)
        assertTrue(api.registrationRequests.isEmpty())
        assertEquals(0, tokenFetchCount)
        assertEquals(null, state.activeRegistration)
        assertTrue(state.pendingUnregistrations.isEmpty())
        assertTrue(state.clearActiveRegistrationCalled)
    }

    @Test
    fun deniedPermissionPersistsCleanupWhenServerRemovalFails() = runTest {
        val state = FakeRegistrationState(
            deviceId = "device-current",
            userId = "user-current"
        )
        val api = FakeRegistrationApi(userId = "user-current").apply {
            unregisterFailure = IOException("server unavailable")
        }
        val repository = repository(
            api = api,
            state = state,
            hasPermission = false,
            token = { error("token must not be fetched when permission is denied") }
        )

        val result = repository.registerCurrentDevice()

        assertEquals(PushRegistrationResult.PERMISSION_REQUIRED_CLEANUP_PENDING, result)
        assertEquals(setOf("device-current" to "user-current"), state.pendingUnregistrations)
        assertEquals(null, state.activeRegistration)
        assertTrue(api.registrationRequests.isEmpty())
    }

    @Test
    fun mismatchedRegistrationOwnerIsNotSavedAndKeepsCleanupState() = runTest {
        val state = FakeRegistrationState(
            deviceId = "device-old",
            userId = "user-current",
            pending = setOf("device-pending" to "other-user")
        )
        val api = FakeRegistrationApi(
            userId = "user-current",
            registration = DeviceRegistrationDto(
                id = "device-returned",
                user_id = "different-user"
            )
        )
        val repository = repository(api = api, state = state, token = { "fcm-token" })

        val result = repository.registerCurrentDevice()

        assertEquals(PushRegistrationResult.BACKEND_UNAVAILABLE, result)
        assertEquals("device-old" to "user-current", state.activeRegistration)
        assertEquals(setOf("device-pending" to "other-user"), state.pendingUnregistrations)
        assertFalse(state.activeRegistrationSaved)
        assertFalse(state.clearPendingTokenCalled)
    }

    @Test
    fun blankRegistrationIdIsNotSavedAndKeepsCleanupState() = runTest {
        val state = FakeRegistrationState(
            deviceId = "device-old",
            userId = "user-current",
            pending = setOf("device-pending" to "other-user")
        )
        val api = FakeRegistrationApi(
            userId = "user-current",
            registration = DeviceRegistrationDto(id = " ", user_id = "user-current")
        )
        val repository = repository(api = api, state = state, token = { "fcm-token" })

        val result = repository.registerCurrentDevice()

        assertEquals(PushRegistrationResult.BACKEND_UNAVAILABLE, result)
        assertEquals("device-old" to "user-current", state.activeRegistration)
        assertEquals(setOf("device-pending" to "other-user"), state.pendingUnregistrations)
        assertFalse(state.activeRegistrationSaved)
        assertFalse(state.clearPendingTokenCalled)
    }

    private fun repository(
        api: FakeRegistrationApi,
        state: FakeRegistrationState,
        hasPermission: Boolean = true,
        token: suspend () -> String?
    ) = PushDeviceRegistrationRepository(
        api = api,
        hasNotificationPermission = { hasPermission },
        currentFcmToken = token,
        state = state,
        appVersion = "test-version"
    )

    private class FakeRegistrationApi(
        private val userId: String,
        private val registration: DeviceRegistrationDto = DeviceRegistrationDto(
            id = "device-new",
            user_id = userId
        )
    ) : PushRegistrationApi {
        val registrationRequests = mutableListOf<DeviceRegistrationRequestDto>()
        val unregisteredDeviceIds = mutableListOf<String>()
        var currentUserFailure: Exception? = null
        var unregisterFailure: Exception? = null

        override suspend fun currentUserId(): String {
            currentUserFailure?.let { throw it }
            return userId
        }

        override suspend fun registerDevice(
            payload: DeviceRegistrationRequestDto
        ): DeviceRegistrationDto {
            registrationRequests += payload
            return registration
        }

        override suspend fun unregisterDevice(deviceId: String) {
            unregisteredDeviceIds += deviceId
            unregisterFailure?.let { throw it }
        }
    }

    private class FakeRegistrationState(
        deviceId: String? = null,
        userId: String? = null,
        pending: Set<Pair<String, String>> = emptySet()
    ) : PushRegistrationStateStore {
        private var registeredDeviceId = deviceId
        private var registeredUserId = userId
        val pendingUnregistrations = pending.toMutableSet()
        var migrationCalled = false
        var clearActiveRegistrationCalled = false
        var clearPendingTokenCalled = false
        var activeRegistrationSaved = false

        val activeRegistration: Pair<String, String>?
            get() = if (registeredDeviceId == null || registeredUserId == null) null
            else registeredDeviceId!! to registeredUserId!!

        override fun migrateLegacyPendingToken() {
            migrationCalled = true
        }

        override fun activeDeviceId(): String? = registeredDeviceId

        override fun activeUserId(): String? = registeredUserId

        override fun saveActiveRegistration(deviceId: String, userId: String) {
            registeredDeviceId = deviceId
            registeredUserId = userId
            activeRegistrationSaved = true
        }

        override fun readPendingUnregistrations(): MutableSet<Pair<String, String>> =
            pendingUnregistrations.toMutableSet()

        override fun writePendingUnregistrations(pending: Set<Pair<String, String>>) {
            pendingUnregistrations.clear()
            pendingUnregistrations.addAll(pending)
        }

        override fun clearActiveRegistration() {
            registeredDeviceId = null
            registeredUserId = null
            clearActiveRegistrationCalled = true
            clearPendingFcmToken()
        }

        override fun clearPendingFcmToken() {
            clearPendingTokenCalled = true
        }
    }
}
