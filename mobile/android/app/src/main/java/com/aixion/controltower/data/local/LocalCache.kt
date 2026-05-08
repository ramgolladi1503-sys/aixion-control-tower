package com.aixion.controltower.data.local

import android.content.Context
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.AuditEventSummary
import com.aixion.controltower.core.model.ProjectSummary
import com.aixion.controltower.core.model.TestRunSummary
import com.aixion.controltower.core.model.WorkOrderSummary
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken

class LocalCache(context: Context) {
    private val prefs = context.getSharedPreferences("aixion_local_cache", Context.MODE_PRIVATE)
    private val gson = Gson()

    fun saveProjects(items: List<ProjectSummary>) = put("projects", items)
    fun getProjects(): List<ProjectSummary> = get("projects")

    fun saveApprovals(items: List<ApprovalSummary>) = put("approvals", items)
    fun getApprovals(): List<ApprovalSummary> = get("approvals")

    fun saveWorkOrders(items: List<WorkOrderSummary>) = put("work_orders", items)
    fun getWorkOrders(): List<WorkOrderSummary> = get("work_orders")

    fun saveTestRuns(items: List<TestRunSummary>) = put("test_runs", items)
    fun getTestRuns(): List<TestRunSummary> = get("test_runs")

    fun saveAuditEvents(items: List<AuditEventSummary>) = put("audit_events", items)
    fun getAuditEvents(): List<AuditEventSummary> = get("audit_events")

    private fun <T> put(key: String, items: List<T>) {
        prefs.edit().putString(key, gson.toJson(items)).apply()
    }

    private inline fun <reified T> get(key: String): List<T> {
        val json = prefs.getString(key, null) ?: return emptyList()
        return runCatching {
            val type = object : TypeToken<List<T>>() {}.type
            gson.fromJson<List<T>>(json, type)
        }.getOrDefault(emptyList())
    }
}
