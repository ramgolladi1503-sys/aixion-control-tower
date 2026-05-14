package com.aixion.controltower.core.api

import android.content.Context
import com.aixion.controltower.BuildConfig
import com.aixion.controltower.core.auth.AuthSession
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory

object OpsApiClient {
    fun create(
        context: Context? = null,
        baseUrl: String = BuildConfig.AIXION_API_BASE_URL,
        accessToken: String = ""
    ): OpsApi {
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
            .create(OpsApi::class.java)
    }
}
