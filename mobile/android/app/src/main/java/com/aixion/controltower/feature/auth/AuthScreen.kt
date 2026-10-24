package com.aixion.controltower.feature.auth

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.focus.FocusDirection
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.BuildConfig
import com.aixion.controltower.core.api.dto.AuthUserDto
import com.aixion.controltower.core.api.dto.InviteDto
import com.aixion.controltower.core.api.dto.SessionDto
import com.aixion.controltower.core.ui.components.ForgedLogoMark
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerHeroPanel
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import com.aixion.controltower.core.ui.theme.TowerPalette
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.towerScreenHorizontalPadding

@Composable
fun AuthScreen(
    viewModel: AuthViewModel = viewModel(),
    onLogoutRequested: () -> Unit = viewModel::logout
) {
    val palette = LocalTowerPalette.current
    val state by viewModel.state.collectAsState()

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(palette.canvas)
            .verticalScroll(rememberScrollState())
            .padding(horizontal = towerScreenHorizontalPadding(), vertical = TowerSpacing.lg),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
    ) {
        if (state.authenticated) {
            AccountHero(state = state)
            NotificationPermissionPanel()
            AccountInfoPanel(
                state = state,
                onLogout = onLogoutRequested
            )
        } else {
            AuthForm(state = state, viewModel = viewModel)
        }
    }
}

@Composable
private fun AccountHero(state: AuthUiState) {
    val palette = LocalTowerPalette.current
    TowerHeroPanel {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.Top
        ) {
            Column(modifier = Modifier.weight(1f)) {
                StatusBadge(
                    label = when {
                        state.authenticated -> "Session active"
                        state.verificationRequired -> "Email verification required"
                        state.emailVerified -> "Ready to log in"
                        else -> "Sign in required"
                    },
                    color = if (state.authenticated || state.emailVerified) palette.positive else palette.caution,
                    uppercase = false
                )
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Text(
                    text = AuthUxCopy.heroTitle(state),
                    color = palette.ink,
                    style = MaterialTheme.typography.headlineLarge
                )
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    text = AuthUxCopy.heroSubtitle(state),
                    color = palette.inkMuted,
                    style = MaterialTheme.typography.bodyMedium
                )
            }
            ForgedLogoMark(size = 52.dp, color = palette.ink.copy(alpha = 0.78f))
        }
    }
}

@Composable
internal fun AccountInfoPanel(
    state: AuthUiState,
    onLogout: () -> Unit
) {
    val palette = LocalTowerPalette.current
    TowerPanel(elevated = true) {
        StatusBadge(label = "Signed in", color = palette.positive, uppercase = false)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text("Account information", color = palette.ink, style = MaterialTheme.typography.titleMedium)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            text = state.userLabel ?: "Current session is active.",
            color = palette.ink,
            style = MaterialTheme.typography.bodyLarge
        )
        state.message?.let { message ->
            AuthFeedbackMessage(message, state.messageSeverity)
        }
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        OutlinedButton(onClick = onLogout, enabled = !state.loading, modifier = Modifier.fillMaxWidth()) {
            Text("Logout")
        }
    }
}

@Composable
private fun AuthForm(state: AuthUiState, viewModel: AuthViewModel) {
    AuthFormContent(
        state = state,
        onEmailChange = viewModel::updateEmail,
        onPasswordChange = viewModel::updatePassword,
        onVerificationCodeChange = viewModel::updateVerificationCode,
        onLogin = viewModel::login,
        onRegister = viewModel::register,
        onVerify = viewModel::verifyEmail,
        onResendVerification = viewModel::resendVerification
    )
}

@Composable
internal fun AuthFormContent(
    state: AuthUiState,
    onEmailChange: (String) -> Unit,
    onPasswordChange: (String) -> Unit,
    onVerificationCodeChange: (String) -> Unit,
    onLogin: () -> Unit,
    onRegister: () -> Unit,
    onVerify: () -> Unit,
    onResendVerification: () -> Unit
) {
    val palette = LocalTowerPalette.current
    var registrationExpanded by rememberSaveable { mutableStateOf(false) }
    // Keep the opt-in reveal only for this composition; recreation starts masked.
    var passwordVisible by remember { mutableStateOf(false) }
    val focusManager = LocalFocusManager.current
    val verificationStarted = state.verificationRequired || state.emailVerified || state.devVerificationCode != null

    LaunchedEffect(verificationStarted) {
        if (verificationStarted) registrationExpanded = true
    }

    Column(verticalArrangement = Arrangement.spacedBy(TowerSpacing.md)) {
        Text(
            text = when {
                state.verificationRequired -> "Verify your email"
                state.emailVerified -> "Email verified"
                registrationExpanded -> "Create your account"
                else -> "Welcome back"
            },
            color = palette.ink,
            style = MaterialTheme.typography.headlineLarge
        )
        Text(
            text = when {
                state.verificationRequired -> "Verify your email to finish creating your account."
                state.emailVerified -> "Log in to review and decide on pending approvals."
                registrationExpanded -> "Create an account once to use Control Tower."
                else -> "Log in to review and decide on pending approvals."
            },
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )

        TowerPanel(elevated = true) {
            OutlinedTextField(
                value = state.email,
                onValueChange = onEmailChange,
                label = { Text("Email address") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                keyboardOptions = KeyboardOptions(
                    keyboardType = KeyboardType.Email,
                    imeAction = ImeAction.Next
                ),
                keyboardActions = KeyboardActions(
                    onNext = { focusManager.moveFocus(FocusDirection.Down) }
                )
            )
            OutlinedTextField(
                value = state.password,
                onValueChange = onPasswordChange,
                label = { Text("Password") },
                supportingText = if (registrationExpanded && !verificationStarted) {
                    { Text(AuthUxCopy.PASSWORD_REQUIREMENTS) }
                } else {
                    null
                },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                keyboardOptions = KeyboardOptions(
                    keyboardType = KeyboardType.Password,
                    imeAction = ImeAction.Done
                ),
                keyboardActions = KeyboardActions(
                    onDone = {
                        if (registrationExpanded && !verificationStarted) {
                            if (AuthUxCopy.canAttemptRegister(state)) onRegister()
                        } else if (AuthUxCopy.canAttemptLogin(state)) {
                            onLogin()
                        }
                    }
                ),
                visualTransformation = if (passwordVisible) {
                    VisualTransformation.None
                } else {
                    PasswordVisualTransformation()
                },
                trailingIcon = {
                    TextButton(
                        onClick = { passwordVisible = !passwordVisible },
                        enabled = !state.loading,
                        modifier = Modifier.semantics {
                            contentDescription = if (passwordVisible) {
                                "Hide password"
                            } else {
                                "Show password"
                            }
                        }
                    ) {
                        Text(if (passwordVisible) "Hide" else "Show")
                    }
                }
            )

            if (registrationExpanded && !verificationStarted) {
                Button(
                    onClick = onRegister,
                    enabled = AuthUxCopy.canAttemptRegister(state),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(if (state.loading) "Creating account..." else "Create account")
                }
                TextButton(
                    onClick = { registrationExpanded = false },
                    enabled = !state.loading,
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text("Back to sign in")
                }
            } else {
                Button(
                    onClick = onLogin,
                    enabled = AuthUxCopy.canAttemptLogin(state),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(AuthUxCopy.primaryLoginLabel(state))
                }
                if (!verificationStarted) {
                    TextButton(
                        onClick = { registrationExpanded = true },
                        enabled = !state.loading,
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text("Create account")
                    }
                }
            }

            if (state.loading && state.message == null) {
                AuthProgressMessage(
                    text = when {
                        verificationStarted -> "Working…"
                        registrationExpanded -> "Creating account…"
                        else -> "Signing in…"
                    }
                )
            }

            state.message?.let { message ->
                AuthFeedbackMessage(message, state.messageSeverity)
            }

            if (verificationStarted) {
                TowerPanel(elevated = false) {
                    Text("Verify your email", color = palette.ink, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
                    Text(AuthUxCopy.verificationGuidance(state), color = palette.inkMuted, fontSize = 14.sp)
                    if (state.emailVerified) {
                        Text("Email verified. Log in to continue.", color = palette.positive, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
                    }
                    developmentVerificationCodeForDisplay(
                        code = state.devVerificationCode,
                        isDebugBuild = BuildConfig.DEBUG
                    )?.let { code ->
                        Text(
                            "Dev verification code: $code",
                            color = palette.positive,
                            fontSize = 14.sp,
                            fontWeight = FontWeight.Bold
                        )
                    }
                    if (!state.emailVerified) {
                        OutlinedTextField(
                            value = state.verificationCode,
                            onValueChange = onVerificationCodeChange,
                            label = { Text("Verification code") },
                            modifier = Modifier.fillMaxWidth(),
                            singleLine = true
                        )
                        Button(
                            onClick = onVerify,
                            enabled = AuthUxCopy.canAttemptVerify(state),
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Text("Verify email")
                        }
                        TextButton(
                            onClick = onResendVerification,
                            enabled = AuthUxCopy.canAttemptResend(state),
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Text("Resend code")
                        }
                    }
                }
            }
        }
    }
}

@Composable
internal fun AuthProgressMessage(text: String) {
    val palette = LocalTowerPalette.current
    Text(
        text = text,
        color = palette.inkMuted,
        style = MaterialTheme.typography.bodyMedium,
        modifier = Modifier.semantics {
            liveRegion = LiveRegionMode.Polite
            stateDescription = AuthMessageSeverity.INFO.accessibilityLabel
        }
    )
}

@Composable
internal fun AuthFeedbackMessage(message: String, severity: AuthMessageSeverity) {
    val palette = LocalTowerPalette.current
    Text(
        text = message,
        color = authFeedbackColor(severity, palette),
        style = MaterialTheme.typography.bodyMedium,
        modifier = Modifier.semantics {
            liveRegion = LiveRegionMode.Polite
            stateDescription = severity.accessibilityLabel
        }
    )
}

internal fun authFeedbackColor(severity: AuthMessageSeverity, palette: TowerPalette): Color =
    when (severity) {
        AuthMessageSeverity.INFO -> palette.info
        AuthMessageSeverity.WARNING -> palette.caution
        AuthMessageSeverity.ERROR -> palette.danger
    }

/** Only debug variants may render a verification code returned for local development. */
internal fun developmentVerificationCodeForDisplay(
    code: String?,
    isDebugBuild: Boolean
): String? = code?.takeIf { isDebugBuild && it.isNotBlank() }

@Composable
private fun PrivacyControlsPanel(state: AuthUiState, viewModel: AuthViewModel) {
    val palette = LocalTowerPalette.current
    TowerPanel(elevated = true) {
        Text("Privacy and data controls", color = palette.ink, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
        Text(
            "Privacy policy and account data controls must be available before Play Store submission. Public policy URL is still configured outside the app release.",
            color = palette.inkMuted,
            fontSize = 14.sp,
            lineHeight = 21.sp
        )
        StatusBadge("Privacy policy: draft", palette.brand, uppercase = false)
        Text(
            "Account removal requests disable app access immediately. Audit and security records may be retained or anonymized according to the published retention policy.",
            color = palette.inkMuted,
            fontSize = 14.sp,
            lineHeight = 21.sp
        )
        OutlinedTextField(
            value = state.privacyRemovalReason,
            onValueChange = viewModel::updatePrivacyRemovalReason,
            label = { Text("Optional reason") },
            modifier = Modifier.fillMaxWidth(),
            maxLines = 3
        )
        Button(
            onClick = viewModel::requestPrivacyAccountRemoval,
            enabled = !state.loading,
            modifier = Modifier.fillMaxWidth()
        ) {
            Text(if (state.loading) "Submitting..." else "Request account removal")
        }
    }
}

@Composable
private fun RoleManagementPanel(state: RoleAdminUiState, onRefresh: () -> Unit, onUpdateRole: (String, String) -> Unit) {
    val palette = LocalTowerPalette.current
    TowerPanel(elevated = true) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Owner role management", color = palette.ink, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
                Text("Visible only after login. Non-owners will see backend 403 here.", color = palette.inkMuted, fontSize = 14.sp)
            }
            OutlinedButton(onClick = onRefresh, enabled = !state.loading) { Text(if (state.loading) "Loading..." else "Refresh") }
        }
        state.message?.let { Text(text = it, color = palette.brand, fontSize = 14.sp) }
        state.errorMessage?.let { Text(text = it, color = palette.danger, fontSize = 14.sp, fontWeight = FontWeight.Bold) }
        if (!state.loading && state.users.isEmpty() && state.errorMessage == null) Text("No users loaded yet. Tap Refresh after logging in as OWNER.", color = palette.inkMuted, fontSize = 14.sp)
        state.users.forEach { user -> RoleUserCard(user, state.roles.ifEmpty { listOf("OWNER", "MAINTAINER", "REVIEWER") }, state.updatingUserId == user.id, onUpdateRole) }
    }
}

@Composable
private fun InviteManagementPanel(state: InviteAdminUiState, roles: List<String>, onEmailChange: (String) -> Unit, onRoleChange: (String) -> Unit, onCreate: () -> Unit, onRefresh: () -> Unit, onRevoke: (String) -> Unit) {
    val palette = LocalTowerPalette.current
    TowerPanel(elevated = true) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Owner invite management", color = palette.ink, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
                Text("Create, list, and revoke invites from the phone. Non-owners will see backend 403 here.", color = palette.inkMuted, fontSize = 14.sp)
            }
            OutlinedButton(onClick = onRefresh, enabled = !state.loading) { Text(if (state.loading) "Loading..." else "Refresh") }
        }
        OutlinedTextField(value = state.inviteEmail, onValueChange = onEmailChange, label = { Text("Invite email") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            roles.forEach { role -> OutlinedButton(onClick = { onRoleChange(role) }, enabled = !state.loading, modifier = Modifier.weight(1f)) { Text(if (state.selectedRole == role) "✓ ${role.take(5)}" else role.take(5)) } }
        }
        Button(onClick = onCreate, enabled = !state.loading && state.inviteEmail.isNotBlank(), modifier = Modifier.fillMaxWidth()) { Text(if (state.loading) "Working..." else "Create invite") }
        state.latestInviteCode?.let { Text("Invite code shown once: $it", color = palette.positive, fontSize = 14.sp, fontWeight = FontWeight.Bold) }
        state.message?.let { Text(text = it, color = palette.brand, fontSize = 14.sp) }
        state.errorMessage?.let { Text(text = it, color = palette.danger, fontSize = 14.sp, fontWeight = FontWeight.Bold) }
        if (!state.loading && state.invites.isEmpty() && state.errorMessage == null) Text("No invites loaded yet.", color = palette.inkMuted, fontSize = 14.sp)
        state.invites.forEach { invite -> InviteCard(invite, state.loading, onRevoke) }
    }
}

@Composable
private fun SessionManagementPanel(state: SessionAdminUiState, onRefresh: () -> Unit, onExpireAccess: (String) -> Unit) {
    val palette = LocalTowerPalette.current
    TowerPanel(elevated = true) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Owner access management", color = palette.ink, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
                Text("List active sessions and expire another user's access. Non-owners will see backend 403 here.", color = palette.inkMuted, fontSize = 14.sp)
            }
            OutlinedButton(onClick = onRefresh, enabled = !state.loading) { Text(if (state.loading) "Loading..." else "Refresh") }
        }
        state.message?.let { Text(text = it, color = palette.brand, fontSize = 14.sp) }
        state.errorMessage?.let { Text(text = it, color = palette.danger, fontSize = 14.sp, fontWeight = FontWeight.Bold) }
        if (!state.loading && state.sessions.isEmpty() && state.errorMessage == null) Text("No sessions loaded yet.", color = palette.inkMuted, fontSize = 14.sp)
        state.sessions.forEach { session -> SessionCard(session, state.loading, state.clearingUserId == session.user_id, onExpireAccess) }
    }
}

@Composable
private fun InviteCard(invite: InviteDto, loading: Boolean, onRevoke: (String) -> Unit) {
    val palette = LocalTowerPalette.current
    TowerPanel(elevated = false) {
        Text(invite.email, color = palette.ink, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
        Text("Role: ${invite.role} • Status: ${invite.status}", color = palette.brand, fontSize = 14.sp)
        invite.accepted_by_user_id?.let { Text("Accepted by: $it", color = palette.inkMuted, fontSize = 14.sp) }
        if (invite.status == "PENDING") OutlinedButton(onClick = { onRevoke(invite.id) }, enabled = !loading) { Text("Revoke") }
    }
}

@Composable
private fun SessionCard(session: SessionDto, loading: Boolean, clearing: Boolean, onExpireAccess: (String) -> Unit) {
    val palette = LocalTowerPalette.current
    TowerPanel(elevated = false) {
        Text(session.user_email, color = palette.ink, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
        Text("Role: ${session.user_role} • Active: ${session.active} • Revoked: ${session.revoked}", color = if (session.active) palette.positive else palette.inkMuted, fontSize = 14.sp)
        Text("Session: ${session.id}", color = palette.inkMuted, fontSize = 14.sp)
        session.expires_at?.let { Text("Expires: $it", color = palette.inkMuted, fontSize = 14.sp) }
        if (session.active) OutlinedButton(onClick = { onExpireAccess(session.user_id) }, enabled = !loading) { Text(if (clearing) "Expiring..." else "Expire user access") }
    }
}

@Composable
private fun RoleUserCard(user: AuthUserDto, roles: List<String>, updating: Boolean, onUpdateRole: (String, String) -> Unit) {
    val palette = LocalTowerPalette.current
    TowerPanel(elevated = false) {
        Text(user.display_name.ifBlank { user.email }, color = palette.ink, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
        Text(user.email, color = palette.inkMuted, fontSize = 14.sp)
        Text("Current role: ${user.role}", color = palette.brand, fontSize = 14.sp)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            roles.forEach { role -> OutlinedButton(onClick = { onUpdateRole(user.id, role) }, enabled = !updating && role != user.role, modifier = Modifier.weight(1f)) { Text(if (updating && role != user.role) "..." else role.take(5)) } }
        }
    }
}
