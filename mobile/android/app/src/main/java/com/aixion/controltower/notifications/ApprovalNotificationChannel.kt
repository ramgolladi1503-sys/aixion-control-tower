package com.aixion.controltower.notifications

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.os.Build
import com.aixion.controltower.R

object ApprovalNotificationChannel {
    const val ID = "aixion_agent_tasks"

    fun ensureCreated(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return

        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
            ?: return
        if (manager.getNotificationChannel(ID) != null) return

        val channel = NotificationChannel(
            ID,
            context.getString(R.string.aixion_agent_tasks_channel_name),
            NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = context.getString(R.string.aixion_agent_tasks_channel_description)
        }
        manager.createNotificationChannel(channel)
    }
}
