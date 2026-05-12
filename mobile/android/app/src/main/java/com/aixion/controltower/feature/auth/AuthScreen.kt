package com.aixion.controltower.feature.auth

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.api.dto.AuthUserDto
import com.aixion.controltower.core.api.dto.InviteDto
import com.aixion.controltower.core.api.dto.SessionDto
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSurface
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
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        Text("Account", color = TowerTextPrimary, fontSize = 28.sp, fontWeight = FontWeight.Bold)
        Text("Connect Android to the authenticated Control Tower backend.", color = TowerTextMuted, fontSize = 14.sp)

        Column(
            modifier = Modifier.fillMaxWidth().background(TowerSurface, RoundedCornerShape(22.dp)).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            Text(
                text = if (state.authenticated) "Session active" else "No active session",
                color = if (state.authenticated) RiskLow else TowerAccent,
                fontSize = 16.sp,
                fontWeight = FontWeight.Bold
            )
            state.userLabel?.let { Text(text = it, color = TowerTextPrimary, fontSize = 14.sp) }
            state.message?.let { Text(text = it, color = TowerAccent, fontSize = 13.sp) }
        }

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
            Button(onClick = viewModel::login, enabled = !state.loading, modifier = Modifier.weight(1f)) { Text(if (state.loading) "Working..." else "Login") }
            OutlinedButton(onClick = viewModel::register, enabled = !state.loading && state.password.length >= 12, modifier = Modifier.weight(1f)) { Text("Register") }
        }

        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            OutlinedButton(onClick = viewModel::refreshSession, enabled = !state.loading, modifier = Modifier.weight(1f)) { Text("Verify session") }
            OutlinedButton(onClick = viewModel::logout, enabled = !state.loading, modifier = Modifier.weight(1f)) { Text("Clear session") }
        }

        if (state.authenticated) {
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
private fun RoleManagementPanel(state: RoleAdminUiState, onRefresh: () -> Unit, onUpdateRole: (String, String) -> Unit) {
    Column(modifier = Modifier.fillMaxWidth().background(TowerSurface, RoundedCornerShape(22.dp)).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Owner role management", color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.Bold)
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
    Column(modifier = Modifier.fillMaxWidth().background(TowerSurface, RoundedCornerShape(22.dp)).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Owner invite management", color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.Bold)
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
    Column(modifier = Modifier.fillMaxWidth().background(TowerSurface, RoundedCornerShape(22.dp)).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Owner access management", color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.Bold)
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
    Column(modifier = Modifier.fillMaxWidth().background(TowerBackground, RoundedCornerShape(18.dp)).padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(invite.email, color = TowerTextPrimary, fontSize = 15.sp, fontWeight = FontWeight.Bold)
        Text("Role: ${invite.role} • Status: ${invite.status}", color = TowerAccent, fontSize = 13.sp)
        invite.accepted_by_user_id?.let { Text("Accepted by: $it", color = TowerTextMuted, fontSize = 12.sp) }
        if (invite.status == "PENDING") OutlinedButton(onClick = { onRevoke(invite.id) }, enabled = !loading) { Text("Revoke") }
    }
}

@Composable
private fun SessionCard(session: SessionDto, loading: Boolean, clearing: Boolean, onExpireAccess: (String) -> Unit) {
    Column(modifier = Modifier.fillMaxWidth().background(TowerBackground, RoundedCornerShape(18.dp)).padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(session.user_email, color = TowerTextPrimary, fontSize = 15.sp, fontWeight = FontWeight.Bold)
        Text("Role: ${session.user_role} • Active: ${session.active} • Revoked: ${session.revoked}", color = if (session.active) RiskLow else TowerTextMuted, fontSize = 13.sp)
        Text("Session: ${session.id}", color = TowerTextMuted, fontSize = 12.sp)
        session.expires_at?.let { Text("Expires: $it", color = TowerTextMuted, fontSize = 12.sp) }
        if (session.active) OutlinedButton(onClick = { onExpireAccess(session.user_id) }, enabled = !loading) { Text(if (clearing) "Expiring..." else "Expire user access") }
    }
}

@Composable
private fun RoleUserCard(user: AuthUserDto, roles: List<String>, updating: Boolean, onUpdateRole: (String, String) -> Unit) {
    Column(modifier = Modifier.fillMaxWidth().background(TowerBackground, RoundedCornerShape(18.dp)).padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(user.display_name.ifBlank { user.email }, color = TowerTextPrimary, fontSize = 15.sp, fontWeight = FontWeight.Bold)
        Text(user.email, color = TowerTextMuted, fontSize = 12.sp)
        Text("Current role: ${user.role}", color = TowerAccent, fontSize = 13.sp)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            roles.forEach { role -> OutlinedButton(onClick = { onUpdateRole(user.id, role) }, enabled = !updating && role != user.role, modifier = Modifier.weight(1f)) { Text(if (updating && role != user.role) "..." else role.take(5)) } }
        }
    }
}
