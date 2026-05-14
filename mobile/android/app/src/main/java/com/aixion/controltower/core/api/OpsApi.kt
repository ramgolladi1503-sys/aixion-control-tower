package com.aixion.controltower.core.api

import retrofit2.http.GET

interface OpsApi {
    @GET("ops/readiness")
    suspend fun getRuntimeReadiness(): Map<String, Any?>
}
