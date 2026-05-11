package com.aixion.controltower.core.auth

import retrofit2.HttpException

object AuthFailure {
    fun isUnauthorized(error: Throwable): Boolean {
        return error is HttpException && error.code() == 401
    }

    fun shouldClearSavedSession(error: Throwable): Boolean {
        return isUnauthorized(error)
    }

    fun operatorMessage(error: Throwable): String {
        return if (isUnauthorized(error)) {
            "Session expired or invalid. Open Acct and log in again."
        } else {
            error.message ?: "backend request failed"
        }
    }
}
