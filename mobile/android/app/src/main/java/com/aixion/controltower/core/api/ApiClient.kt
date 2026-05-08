package com.aixion.controltower.core.api

import android.content.Context
import com.aixion.controltower.core.auth.AuthSession
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory

object ApiClient {
    private const val DEFAULT_BASE_URL = "http://10.0.2.2:8000/"

    fun create(
        context: Context? = null,
        baseUrl: String = DEFAULT_BASE_URL,
        accessToken: String = ""
    ): ControlTowerApi {
        val client = OkHttpClient.Builder()
            .addInterceptor { chain ->
                val resolvedToken = accessToken.ifBlank {
                    context?.let { AuthSession.getAccessToken(it) }.orEmpty()
                }
                val requestBuilder = chain.request().newBuilder()
                if (resolvedToken.isNotBlank()) {
                    requestBuilder.addHeader("Authorization", "Bearer $resolvedToken")
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
