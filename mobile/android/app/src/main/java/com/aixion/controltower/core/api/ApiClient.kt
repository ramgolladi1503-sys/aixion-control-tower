package com.aixion.controltower.core.api

import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory

object ApiClient {
    private const val DEFAULT_BASE_URL = "http://10.0.2.2:8000/"

    fun create(baseUrl: String = DEFAULT_BASE_URL): ControlTowerApi {
        return Retrofit.Builder()
            .baseUrl(baseUrl)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
            .create(ControlTowerApi::class.java)
    }
}
