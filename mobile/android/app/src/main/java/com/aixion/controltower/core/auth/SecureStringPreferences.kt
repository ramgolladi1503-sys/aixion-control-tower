package com.aixion.controltower.core.auth

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.nio.ByteBuffer
import java.nio.charset.StandardCharsets
import java.security.GeneralSecurityException
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Stores small secrets in private preferences encrypted by an Android Keystore key. */
internal object SecureStringPreferences {
    private const val KEYSTORE_PROVIDER = "AndroidKeyStore"
    private const val TRANSFORMATION = "AES/GCM/NoPadding"
    private const val STORAGE_PREFIX = "enc:v1:"
    private const val GCM_TAG_BITS = 128
    private const val IV_BYTES = 12

    private val lock = Any()

    /** Write an encrypted value synchronously; a blank value removes it. */
    fun put(
        context: Context,
        preferencesName: String,
        preferenceKey: String,
        keyAlias: String,
        value: String
    ) {
        synchronized(lock) {
            if (value.isBlank()) {
                removeLocked(context, preferencesName, preferenceKey, keyAlias)
                return
            }
            val encoded = encrypt(value, getOrCreateKey(keyAlias))
            if (!preferences(context, preferencesName).edit()
                    .putString(preferenceKey, encoded)
                    .commit()
            ) {
                throw IllegalStateException("Unable to persist the encrypted value.")
            }
        }
    }

    /** Read a value and encrypt any legacy plaintext before returning it. */
    fun get(
        context: Context,
        preferencesName: String,
        preferenceKey: String,
        keyAlias: String
    ): String = synchronized(lock) {
        val preferences = preferences(context, preferencesName)
        val stored = preferences.getString(preferenceKey, null)?.takeIf(String::isNotBlank)
            ?: return@synchronized ""
        if (!stored.startsWith(STORAGE_PREFIX)) {
            return@synchronized migrateLegacyValue(
                context, preferencesName, preferenceKey, keyAlias, stored
            )
        }
        try {
            decrypt(stored.removePrefix(STORAGE_PREFIX), keyAlias)
        } catch (_: Exception) {
            removeLocked(context, preferencesName, preferenceKey, keyAlias)
            ""
        }
    }

    /** Remove the stored value and its non-exportable encryption key. */
    fun remove(
        context: Context,
        preferencesName: String,
        preferenceKey: String,
        keyAlias: String
    ) {
        synchronized(lock) {
            removeLocked(context, preferencesName, preferenceKey, keyAlias)
        }
    }

    private fun migrateLegacyValue(
        context: Context,
        preferencesName: String,
        preferenceKey: String,
        keyAlias: String,
        legacyValue: String
    ): String {
        return try {
            val encoded = encrypt(legacyValue, getOrCreateKey(keyAlias))
            if (!preferences(context, preferencesName).edit()
                    .putString(preferenceKey, encoded)
                    .commit()
            ) {
                removeLocked(context, preferencesName, preferenceKey, keyAlias)
                ""
            } else {
                legacyValue
            }
        } catch (_: Exception) {
            removeLocked(context, preferencesName, preferenceKey, keyAlias)
            ""
        }
    }

    private fun encrypt(value: String, key: SecretKey): String {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key)
        val encrypted = cipher.doFinal(value.toByteArray(StandardCharsets.UTF_8))
        val packed = ByteBuffer.allocate(cipher.iv.size + encrypted.size)
            .put(cipher.iv)
            .put(encrypted)
            .array()
        return STORAGE_PREFIX + Base64.encodeToString(packed, Base64.NO_WRAP)
    }

    private fun decrypt(encoded: String, keyAlias: String): String {
        val packed = Base64.decode(encoded, Base64.NO_WRAP)
        if (packed.size <= IV_BYTES) {
            throw GeneralSecurityException("Encrypted value is incomplete.")
        }
        val buffer = ByteBuffer.wrap(packed)
        val iv = ByteArray(IV_BYTES)
        buffer.get(iv)
        val ciphertext = ByteArray(buffer.remaining())
        buffer.get(ciphertext)
        val key = getExistingKey(keyAlias)
            ?: throw GeneralSecurityException("Encryption key is unavailable.")
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(GCM_TAG_BITS, iv))
        return String(cipher.doFinal(ciphertext), StandardCharsets.UTF_8)
    }

    private fun getOrCreateKey(keyAlias: String): SecretKey {
        getExistingKey(keyAlias)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE_PROVIDER)
        generator.init(
            KeyGenParameterSpec.Builder(
                keyAlias,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .setKeySize(256)
                .build()
        )
        return generator.generateKey()
    }

    private fun getExistingKey(keyAlias: String): SecretKey? {
        val keyStore = KeyStore.getInstance(KEYSTORE_PROVIDER)
        keyStore.load(null)
        return keyStore.getKey(keyAlias, null) as? SecretKey
    }

    private fun removeLocked(
        context: Context,
        preferencesName: String,
        preferenceKey: String,
        keyAlias: String
    ) {
        val cleared = preferences(context, preferencesName).edit().remove(preferenceKey).commit()
        val keyStore = KeyStore.getInstance(KEYSTORE_PROVIDER)
        keyStore.load(null)
        if (keyStore.containsAlias(keyAlias)) keyStore.deleteEntry(keyAlias)
        if (!cleared) throw IllegalStateException("Unable to clear the encrypted value.")
    }

    private fun preferences(context: Context, name: String) =
        context.getSharedPreferences(name, Context.MODE_PRIVATE)
}
