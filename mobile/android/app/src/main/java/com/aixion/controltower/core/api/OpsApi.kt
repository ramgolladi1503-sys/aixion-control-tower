package com.aixion.controltower.core.api

import com.aixion.controltower.core.api.dto.RuntimeReadinessDto
import retrofit2.http.GET

interface OpsApi {
    @GET("ops/readiness")
    suspend fun getRuntimeReadiness(): RuntimeReadinessDto
}
