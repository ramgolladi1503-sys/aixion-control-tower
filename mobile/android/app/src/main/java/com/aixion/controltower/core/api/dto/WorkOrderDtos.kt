package com.aixion.controltower.core.api.dto

data class WorkOrderDto(
    val id: String,
    val project_id: String,
    val idea_id: String? = null,
    val goal: String,
    val context: String? = null,
    val tasks: List<String> = emptyList(),
    val affected_areas: List<String> = emptyList(),
    val required_tests: List<String> = emptyList(),
    val rollback_plan: String? = null,
    val risk_level: String? = null
)

data class WorkOrderCreateDto(
    val project_id: String,
    val idea_id: String? = null,
    val goal: String,
    val context: String = "",
    val tasks: List<String> = emptyList(),
    val affected_areas: List<String> = emptyList(),
    val required_tests: List<String> = emptyList(),
    val rollback_plan: String = ""
)
