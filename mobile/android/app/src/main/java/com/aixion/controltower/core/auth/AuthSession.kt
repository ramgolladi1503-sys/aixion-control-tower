package com.aixion.controltower.core.auth

import android.content.Context

/** Stores the mobile bearer token encrypted with a non-exportable Android Keystore key. */
object AuthSession {
    private const val PREFS = "aixion_auth"
    private const val ACCESS_TOKEN = "access_token"
    private const val KEY_ALIAS = "com.aixion.controltower.auth.session.v1"

    /** Persist only after encryption and a synchronous preference commit succeed. */
    fun saveAccessToken(context: Context, token: String) =
        SecureStringPreferences.put(context, PREFS, ACCESS_TOKEN, KEY_ALIAS, token)

    /** Legacy plaintext values are encrypted before they are returned. */
    fun getAccessToken(context: Context): String =
        SecureStringPreferences.get(context, PREFS, ACCESS_TOKEN, KEY_ALIAS)

    /** Removes the saved token and its encryption key on logout or account removal. */
    fun clear(context: Context) =
        SecureStringPreferences.remove(context, PREFS, ACCESS_TOKEN, KEY_ALIAS)
}
