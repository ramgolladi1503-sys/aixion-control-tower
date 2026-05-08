package com.aixion.controltower.core.auth

import android.content.Context

object AuthSession {
    private const val PREFS = "aixion_auth"
    private const val ACCESS_TOKEN = "access_token"

    fun saveAccessToken(context: Context, token: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(ACCESS_TOKEN, token)
            .apply()
    }

    fun getAccessToken(context: Context): String {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(ACCESS_TOKEN, "")
            .orEmpty()
    }

    fun clear(context: Context) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .clear()
            .apply()
    }
}
