package com.aixion.controltower.core.api

import android.content.Context
import com.aixion.controltower.BuildConfig
import com.aixion.controltower.core.auth.AuthSession
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

object ApiClient {
    private val defaultBaseUrl: String
        get() = BuildConfig.AIXION_API_BASE_URL

    fun create(
        context: Context? = null,
        baseUrl: String = defaultBaseUrl,
        accessToken: String = ""
    ): ControlTowerApi {
        val client = newHttpClient()
            .newBuilder()
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
            .baseUrl(BackendConnectionConfig.normalizeBaseUrl(baseUrl))
            .client(client)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
            .create(ControlTowerApi::class.java)
    }

    internal fun newHttpClient(): OkHttpClient {
        val timeout = BuildConfig.AIXION_API_TIMEOUT_SECONDS.toLong()
        return OkHttpClient.Builder()
            .connectTimeout(timeout, TimeUnit.SECONDS)
            .readTimeout(timeout, TimeUnit.SECONDS)
            .writeTimeout(timeout, TimeUnit.SECONDS)
            .callTimeout(timeout, TimeUnit.SECONDS)
            .build()
    }
}
