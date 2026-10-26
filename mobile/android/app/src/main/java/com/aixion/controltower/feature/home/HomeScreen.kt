package com.aixion.controltower.feature.home

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.ui.ForegroundRefreshEffect
import com.aixion.controltower.core.ui.components.ApprovalCard
import com.aixion.controltower.core.ui.components.ForgedLogoMark
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerHeroPanel
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.components.TowerSectionHeader
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.towerScreenHorizontalPadding
import java.text.DateFormat
import java.time.Instant
import java.util.Date

@Composable
fun HomeScreen(
    viewModel: HomeViewModel = viewModel(),
    onApprovalSelected: (ApprovalSummary) -> Unit = {},
    onOpenConnect: () -> Unit = {},
    onOpenApprovals: () -> Unit = {}
) {
    val state by viewModel.state.collectAsState()
    ForegroundRefreshEffect(onRefresh = viewModel::refreshInForeground)

    HomeContent(
        state = state,
        onApprovalSelected = onApprovalSelected,
        onOpenConnect = onOpenConnect,
        onOpenApprovals = onOpenApprovals,
        onRetry = viewModel::refresh
    )
}

@Composable
internal fun HomeContent(
    state: HomeUiState,
    onApprovalSelected: (ApprovalSummary) -> Unit = {},
    onOpenConnect: () -> Unit = {},
    onOpenApprovals: () -> Unit = {},
    onRetry: () -> Unit = {}
) {
    val palette = LocalTowerPalette.current

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(palette.canvas)
            .padding(horizontal = towerScreenHorizontalPadding(), vertical = TowerSpacing.lg),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.md)
    ) {
        item {
            HomeHeader(
                lastSuccessfulSyncAt = state.lastSuccessfulSyncAt
            )
        }

        if (shouldShowHomeStatusHero(state)) {
            item {
                TowerHeroPanel(compact = true) {
                    HomeStatusHero(state = state)
                }
            }
        }

        state.errorMessage?.let { message ->
            item {
                ErrorPanel(
                    statusLabel = "Connection issue",
                    title = "Current approvals unavailable",
                    body = "Check your connection, then retry to load the latest requests.",
                    technicalDetails = message,
                    onRetry = onRetry,
                    retryEnabled = !state.loading
                )
            }
        }

        if (!state.hasError && !state.refreshing && state.hasSuccessfulSync && state.actionRequiredApprovals.isNotEmpty()) {
            item {
                HomeApprovalPreview(
                    approvals = state.actionRequiredApprovals,
                    onApprovalSelected = onApprovalSelected,
                    onOpenApprovals = onOpenApprovals
                )
            }
        }

        if (!state.hasError && !state.refreshing && state.hasSuccessfulSync && state.unsupportedApprovals.isNotEmpty()) {
            item {
                ErrorPanel(
                    statusLabel = "Update needed",
                    title = "Approval data is not recognized",
                    body = "Update the app or backend before deciding. These requests remain visible but cannot be approved.",
                    onRetry = onRetry,
                    retryEnabled = !state.loading
                )
            }
            item {
                TowerSectionHeader(
                    title = "Unsupported approval data",
                    subtitle = "Decision controls are disabled for unrecognized status or risk values."
                )
            }
            items(state.unsupportedApprovals) { approval ->
                ApprovalCard(approval = approval, onClick = { onApprovalSelected(approval) })
            }
        }

        item {
            HomeAgentConnectionPanel(onOpenConnect = onOpenConnect)
        }

        item {
            Spacer(modifier = Modifier.height(TowerSpacing.sm))
        }
    }
}

@Composable
private fun HomeStatusHero(state: HomeUiState) {
    val palette = LocalTowerPalette.current
    BoxWithConstraints(modifier = Modifier.fillMaxWidth()) {
        val showBrandMarkBesideCopy =
            maxWidth >= 380.dp && LocalDensity.current.fontScale < 1.3f

        if (showBrandMarkBesideCopy) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(TowerSpacing.md),
                verticalAlignment = Alignment.Top
            ) {
                HomeStatusHeroCopy(state = state, modifier = Modifier.weight(1f))
                ForgedLogoMark(
                    modifier = Modifier.testTag("home-hero-logo"),
                    size = 56.dp,
                    color = palette.ink.copy(alpha = 0.82f)
                )
            }
        } else {
            HomeStatusHeroCopy(state = state, modifier = Modifier.fillMaxWidth())
        }
    }
}

@Composable
private fun HomeStatusHeroCopy(state: HomeUiState, modifier: Modifier = Modifier) {
    val palette = LocalTowerPalette.current
    Column(modifier = modifier) {
        StatusBadge(
            label = homeHeroStatusLabel(state),
            color = when {
                state.unsupportedApprovals.isNotEmpty() -> palette.danger
                state.actionRequiredCount > 0 -> palette.caution
                state.loading || state.refreshing || !state.hasSuccessfulSync -> palette.caution
                else -> palette.positive
            },
            fontSize = MaterialTheme.typography.labelLarge.fontSize,
            uppercase = false
        )
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Text(
            text = homeHeroTitle(state),
            modifier = Modifier
                .fillMaxWidth()
                .testTag("home-status-title")
                .semantics {
                    heading()
                    liveRegion = LiveRegionMode.Polite
                },
            color = palette.ink,
            style = MaterialTheme.typography.headlineMedium
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            text = homeHeroSubtitle(state),
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
    }
}

@Composable
internal fun HomeAgentConnectionPanel(onOpenConnect: () -> Unit) {
    val palette = LocalTowerPalette.current
    TowerPanel(elevated = false) {
        Text(
            text = "Agent connections",
            modifier = Modifier.semantics { heading() },
            color = palette.ink,
            style = MaterialTheme.typography.titleMedium
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            text = "Set up Antigravity or Codex on the computer running the project. Keep that computer available so a phone decision can return to the waiting task.",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Button(onClick = onOpenConnect, modifier = Modifier.fillMaxWidth()) {
            Text("Connect an agent")
        }
    }
}

@Composable
internal fun HomeApprovalPreview(
    approvals: List<ApprovalSummary>,
    onApprovalSelected: (ApprovalSummary) -> Unit,
    onOpenApprovals: () -> Unit
) {
    Column(verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)) {
        TowerSectionHeader(
            title = "Needs your decision",
            subtitle = "Review the exact request before approving or denying it."
        )
        approvals.take(3).forEach { approval ->
            ApprovalCard(approval = approval, onClick = { onApprovalSelected(approval) })
        }
        TextButton(onClick = onOpenApprovals, modifier = Modifier.fillMaxWidth()) {
            Text("See all approvals")
        }
    }
}

@Composable
private fun HomeHeader(lastSuccessfulSyncAt: Instant?) {
    lastSuccessfulSyncAt?.let { syncedAt -> HomeSyncTimestamp(syncedAt) }
}

@Composable
internal fun HomeSyncTimestamp(syncedAt: Instant) {
    val palette = LocalTowerPalette.current
    Text(
        text = "Last successful sync · ${DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT).format(Date.from(syncedAt))}",
        color = palette.inkMuted,
        style = MaterialTheme.typography.bodySmall,
        modifier = Modifier.padding(top = TowerSpacing.sm)
    )
}

@Composable
private fun ErrorPanel(
    title: String,
    body: String,
    onRetry: () -> Unit,
    retryEnabled: Boolean,
    technicalDetails: String? = null,
    statusLabel: String = "Review needed"
) {
    val palette = LocalTowerPalette.current
    var detailsExpanded by remember(technicalDetails) { mutableStateOf(false) }
    TowerPanel(elevated = true) {
        StatusBadge(label = statusLabel, color = palette.danger, fontSize = 14.sp, uppercase = false)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            text = title,
            modifier = Modifier.semantics { heading() },
            color = palette.ink,
            style = MaterialTheme.typography.titleMedium
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            text = body,
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Button(onClick = onRetry, enabled = retryEnabled) {
            Text("Retry sync")
        }
        if (!technicalDetails.isNullOrBlank()) {
            TextButton(
                onClick = { detailsExpanded = !detailsExpanded },
                modifier = Modifier.fillMaxWidth()
            ) {
                Text(if (detailsExpanded) "Hide technical details" else "Technical details")
            }
            if (detailsExpanded) {
                Text(
                    text = technicalDetails,
                    color = palette.inkMuted,
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier
                        .heightIn(max = 192.dp)
                        .verticalScroll(rememberScrollState())
                )
            }
        }
    }
}
