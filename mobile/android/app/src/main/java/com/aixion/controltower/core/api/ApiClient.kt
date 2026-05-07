package com.aixion.controltower.core.api

import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory

object ApiClient {
    private const val DEFAULT_BASE_URL = "http://10.0.2.2:8000/"
    private const val DEFAULT_API_KEY = ""

    fun create(
        baseUrl: String = DEFAULT_BASE_URL,
        apiKey: String = DEFAULT_API_KEY
    ): ControlTowerApi {
        val client = OkHttpClient.Builder()
            .addInterceptor { chain ->
                val requestBuilder = chain.request().newBuilder()
                if (apiKey.isNotBlank()) {
                    requestBuilder.addHeader("X-Aixion-Api-Key", apiKey)
                }
                chain.proceed(requestBuilder.build())
            }
            .build()

        return Retrofit.Builder()
            .baseUrl(baseUrl)
            .client(client)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
            .create(ControlTowerApi::class.java)
    }
}
