package com.aixion.controltower.feature.auth

object AuthUxCopy {
    const val PASSWORD_REQUIREMENTS = "Use 12+ characters with uppercase, lowercase, number, and symbol. Do not use your email, spaces, or obvious sequences."

    fun heroTitle(state: AuthUiState): String {
        return when {
            state.authenticated -> "Your account"
            state.verificationRequired -> "Check your email"
            state.emailVerified -> "Email verified"
            else -> "Sign in to continue"
        }
    }

    fun heroSubtitle(state: AuthUiState): String {
        return when {
            state.authenticated -> "You are signed in and can review approval requests on this phone."
            state.verificationRequired -> "Registration is created, but app access stays locked until this email is verified."
            state.emailVerified -> "Now log in with the same password to open the Control Tower."
            else -> "Register once, verify your email, then log in before using the app shell."
        }
    }

    fun sessionLabel(state: AuthUiState): String {
        return when {
            state.authenticated -> "Session active"
            state.verificationRequired -> "Verification required"
            state.emailVerified -> "Ready to log in"
            else -> "No active session"
        }
    }

    fun primaryLoginLabel(state: AuthUiState): String {
        return when {
            state.loading -> "Please wait..."
            state.verificationRequired -> "Verify email first"
            else -> "Log in"
        }
    }

    fun verificationGuidance(state: AuthUiState): String {
        val email = state.email.trim().ifBlank { "your email" }
        return "Enter the verification code sent to $email. You cannot log in until this succeeds."
    }

    fun canAttemptLogin(state: AuthUiState): Boolean {
        return !state.loading && !state.verificationRequired && state.email.isNotBlank() && state.password.isNotBlank()
    }

    fun canAttemptRegister(state: AuthUiState): Boolean {
        return !state.loading && state.email.isNotBlank() && state.password.length >= 12
    }

    fun canAttemptVerify(state: AuthUiState): Boolean {
        return !state.loading && state.email.isNotBlank() && state.verificationCode.isNotBlank()
    }

    fun canAttemptResend(state: AuthUiState): Boolean {
        return !state.loading && state.email.isNotBlank() && (state.verificationRequired || state.emailVerified.not())
    }
}
