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
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.api.dto.AuthUserDto
import com.aixion.controltower.core.api.dto.InviteDto
import com.aixion.controltower.core.api.dto.SessionDto
import com.aixion.controltower.core.ui.components.ForgedLogoMark
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerHeroPanel
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary
import com.aixion.controltower.core.ui.theme.towerScreenHorizontalPadding

@Composable
fun AuthScreen(
    viewModel: AuthViewModel = viewModel(),
    onLogoutRequested: () -> Unit = viewModel::logout
) {
    val state by viewModel.state.collectAsState()

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
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
    TowerHeroPanel {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.Top
        ) {
            Column(modifier = Modifier.weight(1f)) {
                StatusBadge(
                    label = when {
                        state.authenticated -> "SESSION ACTIVE"
                        state.verificationRequired -> "EMAIL VERIFICATION REQUIRED"
                        state.emailVerified -> "READY TO LOGIN"
                        else -> "ACCESS REQUIRED"
                    },
                    color = if (state.authenticated || state.emailVerified) RiskLow else RiskMedium
                )
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Text(
                    text = AuthUxCopy.heroTitle(state),
                    color = TowerTextPrimary,
                    fontSize = 28.sp,
                    fontWeight = FontWeight.SemiBold,
                    lineHeight = 32.sp
                )
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    text = AuthUxCopy.heroSubtitle(state),
                    color = TowerTextMuted,
                    fontSize = 14.sp,
                    lineHeight = 20.sp
                )
            }
            ForgedLogoMark(size = 52.dp, color = TowerTextPrimary.copy(alpha = 0.78f))
        }
    }
}

@Composable
private fun AccountInfoPanel(
    state: AuthUiState,
    onLogout: () -> Unit
) {
    TowerPanel(elevated = true) {
        StatusBadge(label = "SIGNED IN", color = RiskLow)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text("Account information", color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            text = state.userLabel ?: "Current session is active.",
            color = TowerTextPrimary,
            fontSize = 14.sp,
            lineHeight = 20.sp
        )
        Text(
            text = "Registration, login, and verification controls are hidden because this session is already active.",
            color = TowerTextMuted,
            fontSize = 14.sp,
            lineHeight = 21.sp
        )
        state.message?.let { Text(text = it, color = TowerAccent, fontSize = 14.sp) }
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
    var registrationExpanded by rememberSaveable { mutableStateOf(false) }
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
            color = TowerTextPrimary,
            fontSize = 26.sp,
            fontWeight = FontWeight.SemiBold
        )
        Text(
            text = when {
                state.verificationRequired -> "Verify your email to finish creating your account."
                state.emailVerified -> "Log in to review and decide on pending approvals."
                registrationExpanded -> "Create an account once to use Control Tower."
                else -> "Log in to review and decide on pending approvals."
            },
            color = TowerTextMuted,
            fontSize = 14.sp
        )

        TowerPanel(elevated = true) {
            OutlinedTextField(
                value = state.email,
                onValueChange = onEmailChange,
                label = { Text("Email address") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true
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
                visualTransformation = PasswordVisualTransformation()
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

            state.message?.let { Text(text = it, color = TowerAccent, fontSize = 14.sp) }

            if (verificationStarted) {
                TowerPanel(elevated = false) {
                    Text("Verify your email", color = TowerTextPrimary, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
                    Text(AuthUxCopy.verificationGuidance(state), color = TowerTextMuted, fontSize = 14.sp)
                    if (state.emailVerified) {
                        Text("Email verified. Log in to continue.", color = RiskLow, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
                    }
                    state.devVerificationCode?.let { Text("Dev verification code: $it", color = RiskLow, fontSize = 14.sp, fontWeight = FontWeight.Bold) }
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
private fun PrivacyControlsPanel(state: AuthUiState, viewModel: AuthViewModel) {
    TowerPanel(elevated = true) {
        Text("Privacy and data controls", color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
        Text(
            "Privacy policy and account data controls must be available before Play Store submission. Public policy URL is still configured outside the app release.",
            color = TowerTextMuted,
            fontSize = 14.sp,
            lineHeight = 21.sp
        )
        StatusBadge("PRIVACY POLICY: DRAFT", TowerAccent)
        Text(
            "Account removal requests disable app access immediately. Audit and security records may be retained or anonymized according to the published retention policy.",
            color = TowerTextMuted,
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
    TowerPanel(elevated = true) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Owner role management", color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
                Text("Visible only after login. Non-owners will see backend 403 here.", color = TowerTextMuted, fontSize = 14.sp)
            }
            OutlinedButton(onClick = onRefresh, enabled = !state.loading) { Text(if (state.loading) "Loading..." else "Refresh") }
        }
        state.message?.let { Text(text = it, color = TowerAccent, fontSize = 14.sp) }
        state.errorMessage?.let { Text(text = it, color = RiskCritical, fontSize = 14.sp, fontWeight = FontWeight.Bold) }
        if (!state.loading && state.users.isEmpty() && state.errorMessage == null) Text("No users loaded yet. Tap Refresh after logging in as OWNER.", color = TowerTextMuted, fontSize = 14.sp)
        state.users.forEach { user -> RoleUserCard(user, state.roles.ifEmpty { listOf("OWNER", "MAINTAINER", "REVIEWER") }, state.updatingUserId == user.id, onUpdateRole) }
    }
}

@Composable
private fun InviteManagementPanel(state: InviteAdminUiState, roles: List<String>, onEmailChange: (String) -> Unit, onRoleChange: (String) -> Unit, onCreate: () -> Unit, onRefresh: () -> Unit, onRevoke: (String) -> Unit) {
    TowerPanel(elevated = true) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Owner invite management", color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
                Text("Create, list, and revoke invites from the phone. Non-owners will see backend 403 here.", color = TowerTextMuted, fontSize = 14.sp)
            }
            OutlinedButton(onClick = onRefresh, enabled = !state.loading) { Text(if (state.loading) "Loading..." else "Refresh") }
        }
        OutlinedTextField(value = state.inviteEmail, onValueChange = onEmailChange, label = { Text("Invite email") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            roles.forEach { role -> OutlinedButton(onClick = { onRoleChange(role) }, enabled = !state.loading, modifier = Modifier.weight(1f)) { Text(if (state.selectedRole == role) "✓ ${role.take(5)}" else role.take(5)) } }
        }
        Button(onClick = onCreate, enabled = !state.loading && state.inviteEmail.isNotBlank(), modifier = Modifier.fillMaxWidth()) { Text(if (state.loading) "Working..." else "Create invite") }
        state.latestInviteCode?.let { Text("Invite code shown once: $it", color = RiskLow, fontSize = 14.sp, fontWeight = FontWeight.Bold) }
        state.message?.let { Text(text = it, color = TowerAccent, fontSize = 14.sp) }
        state.errorMessage?.let { Text(text = it, color = RiskCritical, fontSize = 14.sp, fontWeight = FontWeight.Bold) }
        if (!state.loading && state.invites.isEmpty() && state.errorMessage == null) Text("No invites loaded yet.", color = TowerTextMuted, fontSize = 14.sp)
        state.invites.forEach { invite -> InviteCard(invite, state.loading, onRevoke) }
    }
}

@Composable
private fun SessionManagementPanel(state: SessionAdminUiState, onRefresh: () -> Unit, onExpireAccess: (String) -> Unit) {
    TowerPanel(elevated = true) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Owner access management", color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
                Text("List active sessions and expire another user's access. Non-owners will see backend 403 here.", color = TowerTextMuted, fontSize = 14.sp)
            }
            OutlinedButton(onClick = onRefresh, enabled = !state.loading) { Text(if (state.loading) "Loading..." else "Refresh") }
        }
        state.message?.let { Text(text = it, color = TowerAccent, fontSize = 14.sp) }
        state.errorMessage?.let { Text(text = it, color = RiskCritical, fontSize = 14.sp, fontWeight = FontWeight.Bold) }
        if (!state.loading && state.sessions.isEmpty() && state.errorMessage == null) Text("No sessions loaded yet.", color = TowerTextMuted, fontSize = 14.sp)
        state.sessions.forEach { session -> SessionCard(session, state.loading, state.clearingUserId == session.user_id, onExpireAccess) }
    }
}

@Composable
private fun InviteCard(invite: InviteDto, loading: Boolean, onRevoke: (String) -> Unit) {
    TowerPanel(elevated = false) {
        Text(invite.email, color = TowerTextPrimary, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
        Text("Role: ${invite.role} • Status: ${invite.status}", color = TowerAccent, fontSize = 14.sp)
        invite.accepted_by_user_id?.let { Text("Accepted by: $it", color = TowerTextMuted, fontSize = 14.sp) }
        if (invite.status == "PENDING") OutlinedButton(onClick = { onRevoke(invite.id) }, enabled = !loading) { Text("Revoke") }
    }
}

@Composable
private fun SessionCard(session: SessionDto, loading: Boolean, clearing: Boolean, onExpireAccess: (String) -> Unit) {
    TowerPanel(elevated = false) {
        Text(session.user_email, color = TowerTextPrimary, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
        Text("Role: ${session.user_role} • Active: ${session.active} • Revoked: ${session.revoked}", color = if (session.active) RiskLow else TowerTextMuted, fontSize = 14.sp)
        Text("Session: ${session.id}", color = TowerTextMuted, fontSize = 14.sp)
        session.expires_at?.let { Text("Expires: $it", color = TowerTextMuted, fontSize = 14.sp) }
        if (session.active) OutlinedButton(onClick = { onExpireAccess(session.user_id) }, enabled = !loading) { Text(if (clearing) "Expiring..." else "Expire user access") }
    }
}

@Composable
private fun RoleUserCard(user: AuthUserDto, roles: List<String>, updating: Boolean, onUpdateRole: (String, String) -> Unit) {
    TowerPanel(elevated = false) {
        Text(user.display_name.ifBlank { user.email }, color = TowerTextPrimary, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
        Text(user.email, color = TowerTextMuted, fontSize = 14.sp)
        Text("Current role: ${user.role}", color = TowerAccent, fontSize = 14.sp)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            roles.forEach { role -> OutlinedButton(onClick = { onUpdateRole(user.id, role) }, enabled = !updating && role != user.role, modifier = Modifier.weight(1f)) { Text(if (updating && role != user.role) "..." else role.take(5)) } }
        }
    }
}
