package com.aixion.controltower.core.auth

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.security.KeyStore

@RunWith(AndroidJUnit4::class)
class AuthSessionSecurityTest {
    private val context: Context
        get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Before
    fun clearPriorSession() {
        AuthSession.clear(context)
        clearPushToken()
    }

    @After
    fun clearSessionAfterTest() {
        AuthSession.clear(context)
        clearPushToken()
    }

    @Test
    fun savedTokenIsEncryptedAtRestAndCanBeReadBack() {
        val token = "test-bearer-token-that-must-not-be-stored-in-plaintext"

        AuthSession.saveAccessToken(context, token)

        val stored = preferences().getString("access_token", null)
        assertNotNull(stored)
        assertNotEquals(token, stored)
        assertTrue(stored!!.startsWith("enc:v1:"))
        assertEquals(token, AuthSession.getAccessToken(context))
    }

    @Test
    fun legacyPlaintextTokenIsMigratedBeforeItIsReturned() {
        val token = "legacy-session-token"
        preferences().edit().putString("access_token", token).commit()

        assertEquals(token, AuthSession.getAccessToken(context))

        val migrated = preferences().getString("access_token", null)
        assertNotNull(migrated)
        assertNotEquals(token, migrated)
        assertTrue(migrated!!.startsWith("enc:v1:"))
    }

    @Test
    fun corruptCiphertextClearsSessionAndReturnsUnauthenticated() {
        preferences().edit().putString("access_token", "enc:v1:not-valid-base64!").commit()

        assertEquals("", AuthSession.getAccessToken(context))
        assertFalse(preferences().contains("access_token"))
    }

    @Test
    fun missingKeystoreKeyClearsEncryptedSessionAndReturnsUnauthenticated() {
        AuthSession.saveAccessToken(context, "session-token-with-lost-key")
        val keyStore = KeyStore.getInstance("AndroidKeyStore")
        keyStore.load(null)
        keyStore.deleteEntry("com.aixion.controltower.auth.session.v1")

        assertEquals("", AuthSession.getAccessToken(context))
        assertFalse(preferences().contains("access_token"))
        keyStore.load(null)
        assertFalse(keyStore.containsAlias("com.aixion.controltower.auth.session.v1"))
    }

    @Test
    fun clearRemovesTokenAndKeystoreKey() {
        AuthSession.saveAccessToken(context, "session-token")

        AuthSession.clear(context)

        assertFalse(preferences().contains("access_token"))
        assertEquals("", AuthSession.getAccessToken(context))
        val keyStore = KeyStore.getInstance("AndroidKeyStore")
        keyStore.load(null)
        assertFalse(keyStore.containsAlias("com.aixion.controltower.auth.session.v1"))
    }

    @Test
    fun pendingFcmTokenIsEncryptedAtRestAndCanBeReadBack() {
        val token = "fcm-registration-token-that-must-not-be-stored-in-plaintext"

        SecureStringPreferences.put(
            context, PUSH_PREFERENCES, PENDING_FCM_TOKEN, PUSH_TOKEN_KEY_ALIAS, token
        )

        val stored = pushPreferences().getString(PENDING_FCM_TOKEN, null)
        assertNotNull(stored)
        assertNotEquals(token, stored)
        assertTrue(stored!!.startsWith("enc:v1:"))
        assertEquals(
            token,
            SecureStringPreferences.get(
                context, PUSH_PREFERENCES, PENDING_FCM_TOKEN, PUSH_TOKEN_KEY_ALIAS
            )
        )
    }

    @Test
    fun legacyPlaintextFcmTokenMigratesBeforeItIsReturned() {
        val token = "legacy-fcm-registration-token"
        pushPreferences().edit().putString(PENDING_FCM_TOKEN, token).commit()

        assertEquals(
            token,
            SecureStringPreferences.get(
                context, PUSH_PREFERENCES, PENDING_FCM_TOKEN, PUSH_TOKEN_KEY_ALIAS
            )
        )

        val migrated = pushPreferences().getString(PENDING_FCM_TOKEN, null)
        assertNotNull(migrated)
        assertNotEquals(token, migrated)
        assertTrue(migrated!!.startsWith("enc:v1:"))
    }

    @Test
    fun clearingPendingFcmTokenRemovesItsKeystoreKey() {
        SecureStringPreferences.put(
            context, PUSH_PREFERENCES, PENDING_FCM_TOKEN,
            PUSH_TOKEN_KEY_ALIAS, "fcm-registration-token"
        )

        SecureStringPreferences.remove(
            context, PUSH_PREFERENCES, PENDING_FCM_TOKEN, PUSH_TOKEN_KEY_ALIAS
        )

        assertFalse(pushPreferences().contains(PENDING_FCM_TOKEN))
        val keyStore = KeyStore.getInstance("AndroidKeyStore")
        keyStore.load(null)
        assertFalse(keyStore.containsAlias(PUSH_TOKEN_KEY_ALIAS))
    }

    private fun preferences() = context.getSharedPreferences("aixion_auth", Context.MODE_PRIVATE)

    private fun pushPreferences() = context.getSharedPreferences(PUSH_PREFERENCES, Context.MODE_PRIVATE)

    private fun clearPushToken() = SecureStringPreferences.remove(
        context, PUSH_PREFERENCES, PENDING_FCM_TOKEN, PUSH_TOKEN_KEY_ALIAS
    )

    companion object {
        private const val PUSH_PREFERENCES = "aixion_push"
        private const val PENDING_FCM_TOKEN = "pending_fcm_token"
        private const val PUSH_TOKEN_KEY_ALIAS = "com.aixion.controltower.push.token.v1"
    }
}
