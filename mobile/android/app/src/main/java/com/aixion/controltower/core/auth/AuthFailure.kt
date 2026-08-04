package com.aixion.controltower.core.auth

import retrofit2.HttpException

object AuthFailure {
    fun isUnauthorized(error: Throwable): Boolean {
        return error is HttpException && error.code() == 401
    }

    fun shouldClearSavedSession(error: Throwable): Boolean {
        return isUnauthorized(error)
    }

    fun loginMessage(error: Throwable): String {
        return when {
            error is HttpException && error.code() == 401 ->
                "Invalid email or password. Register first if this is a new local backend."
            error is HttpException && error.code() == 403 ->
                "Email verification is required before login."
            else -> operatorMessage(error)
        }
    }

    fun operatorMessage(error: Throwable): String {
        return if (isUnauthorized(error)) {
            "Session expired or invalid. Open Acct and log in again."
        } else {
            error.message ?: "backend request failed"
        }
    }
}
