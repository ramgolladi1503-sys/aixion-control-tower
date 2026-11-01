package com.aixion.controltower.core.ui

import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.aixion.controltower.BuildConfig
import kotlinx.coroutines.delay

private const val MAX_REFRESH_BACKOFF_MILLIS = 300_000L

internal fun nextForegroundRefreshDelayMillis(
    currentDelayMillis: Long,
    baseIntervalMillis: Long,
    refreshSucceeded: Boolean?
): Long {
    require(currentDelayMillis > 0 && baseIntervalMillis > 0) {
        "Foreground refresh intervals must be positive."
    }
    return when (refreshSucceeded) {
        true -> baseIntervalMillis
        false -> (currentDelayMillis * 2).coerceAtMost(MAX_REFRESH_BACKOFF_MILLIS)
        null -> currentDelayMillis
    }
}

/** Periodically refreshes visible app data while this screen is foregrounded. */
@Composable
fun ForegroundRefreshEffect(
    onRefresh: suspend () -> Boolean?,
    intervalMillis: Long = BuildConfig.AIXION_APPROVAL_REFRESH_INTERVAL_SECONDS * 1_000L
) {
    require(intervalMillis > 0) { "Foreground refresh interval must be positive." }
    val lifecycleOwner = LocalLifecycleOwner.current
    var isStarted by remember(lifecycleOwner) {
        mutableStateOf(lifecycleOwner.lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED))
    }
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, _ ->
            isStarted = lifecycleOwner.lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    val latestRefresh by rememberUpdatedState(onRefresh)
    LaunchedEffect(isStarted, intervalMillis) {
        if (!isStarted) return@LaunchedEffect
        var currentDelayMillis = intervalMillis
        while (true) {
            currentDelayMillis = nextForegroundRefreshDelayMillis(
                currentDelayMillis = currentDelayMillis,
                baseIntervalMillis = intervalMillis,
                refreshSucceeded = latestRefresh()
            )
            delay(currentDelayMillis)
        }
    }
}
