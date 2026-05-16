package com.aixion.controltower.core.api.dto

data class ProjectDto(
    val id: String,
    val name: String,
    val description: String? = null,
    val mode: String? = null
)

data class ProjectCreateDto(
    val name: String,
    val description: String = "",
    val mode: String = "STRICT",
    val rules: List<String> = emptyList()
)

data class IdeaDto(
    val id: String,
    val project_id: String? = null,
    val title: String,
    val raw_text: String
)

data class IdeaCreateDto(
    val project_id: String? = null,
    val title: String,
    val raw_text: String
)
