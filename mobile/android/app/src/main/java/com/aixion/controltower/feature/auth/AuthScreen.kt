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

        SessionStatePanel(state = state)

        TowerSectionHeader(
            title = "Backend Access",
            subtitle = "Authenticate this Android device against the Control Tower backend before owner controls are available."
        )

        AuthForm(state = state, viewModel = viewModel)

        if (state.authenticated) {
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
                        state.emailVerified -> "EMAIL VERIFIED"
                        else -> "ACCESS REQUIRED"
                    },
                    color = if (state.authenticated || state.emailVerified) RiskLow else RiskMedium
                )
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Text(
                    text = "Account",
                    color = TowerTextPrimary,
                    fontSize = 28.sp,
                    fontWeight = FontWeight.SemiBold,
                    lineHeight = 32.sp
                )
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    text = "Connect Android to the authenticated Control Tower backend.",
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
            label = when {
                state.authenticated -> "Session active"
                state.verificationRequired -> "Verify email before login"
                else -> "No active session"
            },
            color = when {
                state.authenticated -> RiskLow
                state.verificationRequired -> RiskMedium
                else -> TowerAccent
            }
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        state.userLabel?.let { Text(text = it, color = TowerTextPrimary, fontSize = 14.sp) }
        state.message?.let { Text(text = it, color = TowerAccent, fontSize = 13.sp) }
    }
}

@Composable
private fun AuthForm(state: AuthUiState, viewModel: AuthViewModel) {
    TowerPanel(elevated = true) {
        OutlinedTextField(value = state.email, onValueChange = viewModel::updateEmail, label = { Text("Email") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
        OutlinedTextField(
            value = state.password,
            onValueChange = viewModel::updatePassword,
            label = { Text("Password") },
            supportingText = { Text("Registration requires at least 12 characters.") },
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
            Button(onClick = viewModel::login, enabled = !state.loading && !state.verificationRequired, modifier = Modifier.weight(1f)) { Text(if (state.loading) "Working..." else "Login") }
            OutlinedButton(onClick = viewModel::register, enabled = !state.loading && state.password.length >= 12, modifier = Modifier.weight(1f)) { Text("Register") }
        }

        if (state.verificationRequired || state.devVerificationCode != null) {
            TowerPanel(elevated = false) {
                Text("Email verification", color = TowerTextPrimary, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
                Text("Registration does not grant app access until this email is verified.", color = TowerTextMuted, fontSize = 12.sp)
                state.devVerificationCode?.let { Text("Dev verification code: $it", color = RiskLow, fontSize = 13.sp, fontWeight = FontWeight.Bold) }
                OutlinedTextField(
                    value = state.verificationCode,
                    onValueChange = viewModel::updateVerificationCode,
                    label = { Text("Verification code") },
                    modifier = Modifier.fillMaxWidth(),
                    singleLine = true
                )
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
                    Button(onClick = viewModel::verifyEmail, enabled = !state.loading && state.verificationCode.isNotBlank(), modifier = Modifier.weight(1f)) { Text("Verify email") }
                    OutlinedButton(onClick = viewModel::resendVerification, enabled = !state.loading && state.email.isNotBlank(), modifier = Modifier.weight(1f)) { Text("Resend") }
                }
            }
        }

        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            OutlinedButton(onClick = viewModel::refreshSession, enabled = !state.loading, modifier = Modifier.weight(1f)) { Text("Verify session") }
            OutlinedButton(onClick = viewModel::logout, enabled = !state.loading, modifier = Modifier.weight(1f)) { Text("Clear session") }
        }
    }
}
