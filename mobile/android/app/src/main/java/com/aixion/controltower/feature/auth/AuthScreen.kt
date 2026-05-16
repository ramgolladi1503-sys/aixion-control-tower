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
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
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
import com.aixion.controltower.core.ui.components.TowerSectionHeader
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
fun AuthScreen(
    viewModel: AuthViewModel = viewModel(),
    roleAdminViewModel: RoleAdminViewModel = viewModel(),
    inviteAdminViewModel: InviteAdminViewModel = viewModel(),
    sessionAdminViewModel: SessionAdminViewModel = viewModel()
) {
    val state by viewModel.state.collectAsState()
    val roleState by roleAdminViewModel.state.collectAsState()
    val inviteState by inviteAdminViewModel.state.collectAsState()
    val sessionState by sessionAdminViewModel.state.collectAsState()

    LaunchedEffect(state.authenticated) {
        if (state.authenticated) {
            roleAdminViewModel.refresh()
            inviteAdminViewModel.refresh()
            sessionAdminViewModel.refresh()
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .verticalScroll(rememberScrollState())
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
    ) {
        AccountHero(state = state)

        if (state.authenticated) {
            AccountInfoPanel(
                state = state,
                onRefreshSession = viewModel::refreshSession,
                onLogout = viewModel::logout
            )

            PrivacyControlsPanel(state = state, viewModel = viewModel)

            TowerSectionHeader(
                title = "Owner Administration",
                subtitle = "Roles, invites, and sessions are sensitive control surfaces. Every action should stay deliberate and traceable."
            )
            RoleManagementPanel(roleState, roleAdminViewModel::refresh, roleAdminViewModel::updateRole)
            InviteManagementPanel(
                state = inviteState,
                roles = roleState.roles.ifEmpty { listOf("OWNER", "MAINTAINER", "REVIEWER") },
                onEmailChange = inviteAdminViewModel::updateInviteEmail,
                onRoleChange = inviteAdminViewModel::updateSelectedRole,
                onCreate = inviteAdminViewModel::createInvite,
                onRefresh = inviteAdminViewModel::refresh,
                onRevoke = inviteAdminViewModel::revokeInvite
            )
            SessionManagementPanel(
                state = sessionState,
                onRefresh = sessionAdminViewModel::refresh,
                onExpireAccess = sessionAdminViewModel::clearUserSessions
            )
        } else {
            SessionStatePanel(state = state)

            TowerSectionHeader(
                title = "Access flow",
                subtitle = "Use the sequence in order: register, verify email, then log in. The app stays locked until that sequence is complete."
            )

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
private fun SessionStatePanel(state: AuthUiState) {
    TowerPanel(elevated = true) {
        StatusBadge(
            label = AuthUxCopy.sessionLabel(state),
            color = when {
                state.authenticated -> RiskLow
                state.verificationRequired -> RiskMedium
                state.emailVerified -> RiskLow
                else -> TowerAccent
            }
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        state.userLabel?.let { Text(text = it, color = TowerTextPrimary, fontSize = 14.sp) }
        state.message?.let { Text(text = it, color = TowerAccent, fontSize = 13.sp) }
    }
}

@Composable
private fun AccountInfoPanel(
    state: AuthUiState,
    onRefreshSession: () -> Unit,
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
            fontSize = 12.sp,
            lineHeight = 18.sp
        )
        state.message?.let { Text(text = it, color = TowerAccent, fontSize = 13.sp) }
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        OutlinedButton(onClick = onRefreshSession, enabled = !state.loading, modifier = Modifier.fillMaxWidth()) {
            Text(if (state.loading) "Checking session..." else "Refresh session")
        }
        OutlinedButton(onClick = onLogout, enabled = !state.loading, modifier = Modifier.fillMaxWidth()) {
            Text("Log out")
        }
    }
}

@Composable
private fun AuthForm(state: AuthUiState, viewModel: AuthViewModel) {
    TowerPanel(elevated = true) {
        Text("1. Register or log in", color = TowerTextPrimary, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
        Text("Register creates the account only. It does not unlock the app until email verification succeeds.", color = TowerTextMuted, fontSize = 12.sp)
        OutlinedTextField(value = state.email, onValueChange = viewModel::updateEmail, label = { Text("Email") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
        OutlinedTextField(
            value = state.password,
            onValueChange = viewModel::updatePassword,
            label = { Text("Password") },
            supportingText = { Text(AuthUxCopy.PASSWORD_REQUIREMENTS) },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
            visualTransformation = PasswordVisualTransformation()
        )
        OutlinedTextField(value = state.displayName, onValueChange = viewModel::updateDisplayName, label = { Text("Display name for registration") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
        OutlinedTextField(
            value = state.inviteCode,
            onValueChange = viewModel::updateInviteCode,
            label = { Text("Invite code") },
            supportingText = { Text("First owner can register without this. Later users need an owner invite.") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true
        )

        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            Button(onClick = viewModel::login, enabled = AuthUxCopy.canAttemptLogin(state), modifier = Modifier.weight(1f)) { Text(AuthUxCopy.primaryLoginLabel(state)) }
            OutlinedButton(onClick = viewModel::register, enabled = AuthUxCopy.canAttemptRegister(state), modifier = Modifier.weight(1f)) { Text("Register") }
        }

        if (state.verificationRequired || state.devVerificationCode != null || state.emailVerified) {
            TowerPanel(elevated = false) {
                Text("2. Verify email", color = TowerTextPrimary, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
                Text(AuthUxCopy.verificationGuidance(state), color = TowerTextMuted, fontSize = 12.sp)
                if (state.emailVerified) {
                    Text("Email is verified. Use Login now to open the app.", color = RiskLow, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                }
                state.devVerificationCode?.let { Text("Dev verification code: $it", color = RiskLow, fontSize = 13.sp, fontWeight = FontWeight.Bold) }
                OutlinedTextField(
                    value = state.verificationCode,
                    onValueChange = viewModel::updateVerificationCode,
                    label = { Text("Verification code") },
                    modifier = Modifier.fillMaxWidth(),
                    singleLine = true
                )
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
                    Button(onClick = viewModel::verifyEmail, enabled = AuthUxCopy.canAttemptVerify(state), modifier = Modifier.weight(1f)) { Text("Verify email") }
                    OutlinedButton(onClick = viewModel::resendVerification, enabled = AuthUxCopy.canAttemptResend(state), modifier = Modifier.weight(1f)) { Text("Resend code") }
                }
            }
        }

        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            OutlinedButton(onClick = viewModel::refreshSession, enabled = !state.loading, modifier = Modifier.weight(1f)) { Text("Verify saved session") }
            OutlinedButton(onClick = viewModel::logout, enabled = !state.loading, modifier = Modifier.weight(1f)) { Text("Clear session") }
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
            fontSize = 12.sp,
            lineHeight = 18.sp
        )
        StatusBadge("PRIVACY POLICY: DRAFT", TowerAccent)
        Text(
            "Account removal requests disable app access immediately. Audit and security records may be retained or anonymized according to the published retention policy.",
            color = TowerTextMuted,
            fontSize = 12.sp,
            lineHeight = 18.sp
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
                Text("Visible only after login. Non-owners will see backend 403 here.", color = TowerTextMuted, fontSize = 12.sp)
            }
            OutlinedButton(onClick = onRefresh, enabled = !state.loading) { Text(if (state.loading) "Loading..." else "Refresh") }
        }
        state.message?.let { Text(text = it, color = TowerAccent, fontSize = 13.sp) }
        state.errorMessage?.let { Text(text = it, color = RiskCritical, fontSize = 13.sp, fontWeight = FontWeight.Bold) }
        if (!state.loading && state.users.isEmpty() && state.errorMessage == null) Text("No users loaded yet. Tap Refresh after logging in as OWNER.", color = TowerTextMuted, fontSize = 13.sp)
        state.users.forEach { user -> RoleUserCard(user, state.roles.ifEmpty { listOf("OWNER", "MAINTAINER", "REVIEWER") }, state.updatingUserId == user.id, onUpdateRole) }
    }
}

@Composable
private fun InviteManagementPanel(state: InviteAdminUiState, roles: List<String>, onEmailChange: (String) -> Unit, onRoleChange: (String) -> Unit, onCreate: () -> Unit, onRefresh: () -> Unit, onRevoke: (String) -> Unit) {
    TowerPanel(elevated = true) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Owner invite management", color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
                Text("Create, list, and revoke invites from the phone. Non-owners will see backend 403 here.", color = TowerTextMuted, fontSize = 12.sp)
            }
            OutlinedButton(onClick = onRefresh, enabled = !state.loading) { Text(if (state.loading) "Loading..." else "Refresh") }
        }
        OutlinedTextField(value = state.inviteEmail, onValueChange = onEmailChange, label = { Text("Invite email") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            roles.forEach { role -> OutlinedButton(onClick = { onRoleChange(role) }, enabled = !state.loading, modifier = Modifier.weight(1f)) { Text(if (state.selectedRole == role) "✓ ${role.take(5)}" else role.take(5)) } }
        }
        Button(onClick = onCreate, enabled = !state.loading && state.inviteEmail.isNotBlank(), modifier = Modifier.fillMaxWidth()) { Text(if (state.loading) "Working..." else "Create invite") }
        state.latestInviteCode?.let { Text("Invite code shown once: $it", color = RiskLow, fontSize = 13.sp, fontWeight = FontWeight.Bold) }
        state.message?.let { Text(text = it, color = TowerAccent, fontSize = 13.sp) }
        state.errorMessage?.let { Text(text = it, color = RiskCritical, fontSize = 13.sp, fontWeight = FontWeight.Bold) }
        if (!state.loading && state.invites.isEmpty() && state.errorMessage == null) Text("No invites loaded yet.", color = TowerTextMuted, fontSize = 13.sp)
        state.invites.forEach { invite -> InviteCard(invite, state.loading, onRevoke) }
    }
}

@Composable
private fun SessionManagementPanel(state: SessionAdminUiState, onRefresh: () -> Unit, onExpireAccess: (String) -> Unit) {
    TowerPanel(elevated = true) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Owner access management", color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
                Text("List active sessions and expire another user's access. Non-owners will see backend 403 here.", color = TowerTextMuted, fontSize = 12.sp)
            }
            OutlinedButton(onClick = onRefresh, enabled = !state.loading) { Text(if (state.loading) "Loading..." else "Refresh") }
        }
        state.message?.let { Text(text = it, color = TowerAccent, fontSize = 13.sp) }
        state.errorMessage?.let { Text(text = it, color = RiskCritical, fontSize = 13.sp, fontWeight = FontWeight.Bold) }
        if (!state.loading && state.sessions.isEmpty() && state.errorMessage == null) Text("No sessions loaded yet.", color = TowerTextMuted, fontSize = 13.sp)
        state.sessions.forEach { session -> SessionCard(session, state.loading, state.clearingUserId == session.user_id, onExpireAccess) }
    }
}

@Composable
private fun InviteCard(invite: InviteDto, loading: Boolean, onRevoke: (String) -> Unit) {
    TowerPanel(elevated = false) {
        Text(invite.email, color = TowerTextPrimary, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
        Text("Role: ${invite.role} • Status: ${invite.status}", color = TowerAccent, fontSize = 13.sp)
        invite.accepted_by_user_id?.let { Text("Accepted by: $it", color = TowerTextMuted, fontSize = 12.sp) }
        if (invite.status == "PENDING") OutlinedButton(onClick = { onRevoke(invite.id) }, enabled = !loading) { Text("Revoke") }
    }
}

@Composable
private fun SessionCard(session: SessionDto, loading: Boolean, clearing: Boolean, onExpireAccess: (String) -> Unit) {
    TowerPanel(elevated = false) {
        Text(session.user_email, color = TowerTextPrimary, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
        Text("Role: ${session.user_role} • Active: ${session.active} • Revoked: ${session.revoked}", color = if (session.active) RiskLow else TowerTextMuted, fontSize = 13.sp)
        Text("Session: ${session.id}", color = TowerTextMuted, fontSize = 12.sp)
        session.expires_at?.let { Text("Expires: $it", color = TowerTextMuted, fontSize = 12.sp) }
        if (session.active) OutlinedButton(onClick = { onExpireAccess(session.user_id) }, enabled = !loading) { Text(if (clearing) "Expiring..." else "Expire user access") }
    }
}

@Composable
private fun RoleUserCard(user: AuthUserDto, roles: List<String>, updating: Boolean, onUpdateRole: (String, String) -> Unit) {
    TowerPanel(elevated = false) {
        Text(user.display_name.ifBlank { user.email }, color = TowerTextPrimary, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
        Text(user.email, color = TowerTextMuted, fontSize = 12.sp)
        Text("Current role: ${user.role}", color = TowerAccent, fontSize = 13.sp)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            roles.forEach { role -> OutlinedButton(onClick = { onUpdateRole(user.id, role) }, enabled = !updating && role != user.role, modifier = Modifier.weight(1f)) { Text(if (updating && role != user.role) "..." else role.take(5)) } }
        }
    }
}
