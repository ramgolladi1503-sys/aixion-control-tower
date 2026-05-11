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
    roleAdminViewModel: RoleAdminViewModel = viewModel()
) {
    val state by viewModel.state.collectAsState()
    val roleState by roleAdminViewModel.state.collectAsState()

    LaunchedEffect(state.authenticated) {
        if (state.authenticated) {
            roleAdminViewModel.refresh()
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
        Text(
            text = "Account",
            color = TowerTextPrimary,
            fontSize = 28.sp,
            fontWeight = FontWeight.Bold
        )
        Text(
            text = "Connect Android to the authenticated Control Tower backend.",
            color = TowerTextMuted,
            fontSize = 14.sp
        )

        Column(
            modifier = Modifier
                .fillMaxWidth()
                .background(TowerSurface, RoundedCornerShape(22.dp))
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            Text(
                text = if (state.authenticated) "Session active" else "No active session",
                color = if (state.authenticated) RiskLow else TowerAccent,
                fontSize = 16.sp,
                fontWeight = FontWeight.Bold
            )
            state.userLabel?.let { label ->
                Text(text = label, color = TowerTextPrimary, fontSize = 14.sp)
            }
            state.message?.let { message ->
                Text(text = message, color = TowerAccent, fontSize = 13.sp)
            }
        }

        OutlinedTextField(
            value = state.email,
            onValueChange = viewModel::updateEmail,
            label = { Text("Email") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true
        )
        OutlinedTextField(
            value = state.password,
            onValueChange = viewModel::updatePassword,
            label = { Text("Password") },
            supportingText = { Text("Registration requires at least 12 characters.") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
            visualTransformation = PasswordVisualTransformation()
        )
        OutlinedTextField(
            value = state.displayName,
            onValueChange = viewModel::updateDisplayName,
            label = { Text("Display name for registration") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true
        )

        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            Button(
                onClick = viewModel::login,
                enabled = !state.loading,
                modifier = Modifier.weight(1f)
            ) {
                Text(if (state.loading) "Working..." else "Login")
            }
            OutlinedButton(
                onClick = viewModel::register,
                enabled = !state.loading && state.password.length >= 12,
                modifier = Modifier.weight(1f)
            ) {
                Text("Register")
            }
        }

        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            OutlinedButton(
                onClick = viewModel::refreshSession,
                enabled = !state.loading,
                modifier = Modifier.weight(1f)
            ) {
                Text("Verify session")
            }
            OutlinedButton(
                onClick = viewModel::logout,
                enabled = !state.loading,
                modifier = Modifier.weight(1f)
            ) {
                Text("Clear session")
            }
        }

        if (state.authenticated) {
            RoleManagementPanel(
                state = roleState,
                onRefresh = roleAdminViewModel::refresh,
                onUpdateRole = roleAdminViewModel::updateRole
            )
        }
    }
}

@Composable
private fun RoleManagementPanel(
    state: RoleAdminUiState,
    onRefresh: () -> Unit,
    onUpdateRole: (String, String) -> Unit
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(TowerSurface, RoundedCornerShape(22.dp))
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = "Owner role management",
                    color = TowerTextPrimary,
                    fontSize = 18.sp,
                    fontWeight = FontWeight.Bold
                )
                Text(
                    text = "Visible only after login. Non-owners will see backend 403 here.",
                    color = TowerTextMuted,
                    fontSize = 12.sp
                )
            }
            OutlinedButton(onClick = onRefresh, enabled = !state.loading) {
                Text(if (state.loading) "Loading..." else "Refresh")
            }
        }

        state.message?.let { message ->
            Text(text = message, color = TowerAccent, fontSize = 13.sp)
        }
        state.errorMessage?.let { error ->
            Text(text = error, color = RiskCritical, fontSize = 13.sp, fontWeight = FontWeight.Bold)
        }

        if (!state.loading && state.users.isEmpty() && state.errorMessage == null) {
            Text(
                text = "No users loaded yet. Tap Refresh after logging in as OWNER.",
                color = TowerTextMuted,
                fontSize = 13.sp
            )
        }

        state.users.forEach { user ->
            RoleUserCard(
                user = user,
                roles = state.roles.ifEmpty { listOf("OWNER", "MAINTAINER", "REVIEWER") },
                updating = state.updatingUserId == user.id,
                onUpdateRole = onUpdateRole
            )
        }
    }
}

@Composable
private fun RoleUserCard(
    user: AuthUserDto,
    roles: List<String>,
    updating: Boolean,
    onUpdateRole: (String, String) -> Unit
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(TowerBackground, RoundedCornerShape(18.dp))
            .padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        Text(
            text = user.display_name.ifBlank { user.email },
            color = TowerTextPrimary,
            fontSize = 15.sp,
            fontWeight = FontWeight.Bold
        )
        Text(text = user.email, color = TowerTextMuted, fontSize = 12.sp)
        Text(text = "Current role: ${user.role}", color = TowerAccent, fontSize = 13.sp)

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            roles.forEach { role ->
                OutlinedButton(
                    onClick = { onUpdateRole(user.id, role) },
                    enabled = !updating && role != user.role,
                    modifier = Modifier.weight(1f)
                ) {
                    Text(if (updating && role != user.role) "..." else role.take(5))
                }
            }
        }
    }
}
