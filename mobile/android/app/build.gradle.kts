import java.net.URI
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val firebaseConfigEnabled = providers.gradleProperty("AIXION_FIREBASE_CONFIG_ENABLED")
    .orElse("false")
    .map { raw ->
        when (raw.trim().lowercase()) {
            "true" -> true
            "false" -> false
            else -> throw GradleException(
                "AIXION_FIREBASE_CONFIG_ENABLED must be true or false."
            )
        }
    }
    .get()
val firebaseConfigFile = project.file("google-services.json")
if (firebaseConfigEnabled) {
    if (!firebaseConfigFile.isFile) {
        throw GradleException(
            "AIXION_FIREBASE_CONFIG_ENABLED=true requires mobile/android/app/google-services.json."
        )
    }
    apply(plugin = "com.google.gms.google-services")
}

val configuredApiBaseUrl = providers.gradleProperty("AIXION_API_BASE_URL").orNull
val aixionApiBaseUrl = configuredApiBaseUrl ?: "http://10.0.2.2:8000/"
val approvalRefreshIntervalSeconds = providers.gradleProperty("AIXION_APPROVAL_REFRESH_INTERVAL_SECONDS")
    .orElse("15")
    .map { raw ->
        val parsed = raw.toIntOrNull()
            ?: throw GradleException("AIXION_APPROVAL_REFRESH_INTERVAL_SECONDS must be an integer from 5 to 120.")
        if (parsed !in 5..120) {
            throw GradleException("AIXION_APPROVAL_REFRESH_INTERVAL_SECONDS must be an integer from 5 to 120.")
        }
        parsed
    }

val signingPropertiesFile = rootProject.file("keystore.properties")
val signingProperties = Properties().apply {
    if (signingPropertiesFile.exists()) {
        signingPropertiesFile.inputStream().use { load(it) }
    }
}

fun signingValue(name: String): String? {
    return signingProperties.getProperty(name)
        ?: providers.gradleProperty(name).orNull
        ?: providers.environmentVariable(name).orNull
}

val releaseStoreFile = signingValue("ANDROID_KEYSTORE_FILE")?.let { rootProject.file(it) }
val releaseStorePassword = signingValue("ANDROID_KEYSTORE_PASSWORD")
val releaseKeyAlias = signingValue("ANDROID_KEY_ALIAS")
val releaseKeyPassword = signingValue("ANDROID_KEY_PASSWORD")
val hasReleaseSigningConfig = releaseStoreFile?.exists() == true &&
    !releaseStorePassword.isNullOrBlank() &&
    !releaseKeyAlias.isNullOrBlank() &&
    !releaseKeyPassword.isNullOrBlank()

android {
    namespace = "com.aixion.controltower"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.aixion.controltower"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        buildConfigField("String", "AIXION_API_BASE_URL", "\"$aixionApiBaseUrl\"")
        buildConfigField("int", "AIXION_APPROVAL_REFRESH_INTERVAL_SECONDS", approvalRefreshIntervalSeconds.get().toString())
        buildConfigField("boolean", "AIXION_FIREBASE_CONFIG_ENABLED", firebaseConfigEnabled.toString())
        manifestPlaceholders["cleartextTrafficAllowed"] = "false"
    }

    signingConfigs {
        if (hasReleaseSigningConfig) {
            create("release") {
                storeFile = releaseStoreFile
                storePassword = releaseStorePassword
                keyAlias = releaseKeyAlias
                keyPassword = releaseKeyPassword
            }
        }
    }

    buildTypes {
        debug {
            // Keep development and instrumentation installs isolated from the
            // release-identity app and its local session data.
            applicationIdSuffix = ".debug"
            // Local emulator and USB-reverse development endpoints use HTTP.
            // This placeholder is never enabled for release variants.
            manifestPlaceholders["cleartextTrafficAllowed"] = "true"
        }
        release {
            manifestPlaceholders["cleartextTrafficAllowed"] = "false"
            if (hasReleaseSigningConfig) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.15"
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

fun releaseApiUrlProblem(rawUrl: String): String? {
    val parsedUrl = try {
        URI(rawUrl)
    } catch (_: Exception) {
        return "AIXION_API_BASE_URL is not a valid absolute URL."
    }
    val normalizedHost = parsedUrl.host?.lowercase()?.removeSuffix(".").orEmpty()
    val ipv4Address = Regex("^\\d{1,3}(?:\\.\\d{1,3}){3}$")
    val ipv6Address = normalizedHost.contains(":")
    val localOrReservedHost = normalizedHost == "localhost" ||
        normalizedHost.endsWith(".localhost") ||
        normalizedHost == "local" ||
        normalizedHost.endsWith(".local") ||
        normalizedHost == "example.com" ||
        normalizedHost.endsWith(".example.com") ||
        normalizedHost == "example.org" ||
        normalizedHost.endsWith(".example.org") ||
        normalizedHost == "example.net" ||
        normalizedHost.endsWith(".example.net") ||
        normalizedHost.endsWith(".invalid") ||
        normalizedHost.endsWith(".test")
    if (!parsedUrl.scheme.equals("https", ignoreCase = true) ||
        normalizedHost.isBlank() ||
        !normalizedHost.contains(".") ||
        ipv4Address.matches(normalizedHost) ||
        ipv6Address ||
        localOrReservedHost ||
        !rawUrl.endsWith("/") ||
        (parsedUrl.port != -1 && parsedUrl.port !in 1..65535) ||
        parsedUrl.userInfo != null ||
        parsedUrl.query != null ||
        parsedUrl.fragment != null
    ) {
        return "Release builds require an HTTPS API base URL with a public DNS host, a trailing slash, a valid port, and no credentials, query, or fragment."
    }
    return null
}

fun releaseFirebaseConfigProblem(configEnabled: Boolean, configExists: Boolean): String? {
    if (!configEnabled) {
        return "Release builds require -PAIXION_FIREBASE_CONFIG_ENABLED=true for push notifications."
    }
    if (!configExists) {
        return "AIXION_FIREBASE_CONFIG_ENABLED=true requires mobile/android/app/google-services.json."
    }
    return null
}

tasks.register("verifyReleaseApiUrlGuard") {
    group = "verification"
    description = "Checks accepted and rejected release API base URL examples."
    doLast {
        val accepted = listOf("https://aixionlab.com/")
        val rejected = listOf(
            "http://aixionlab.com/",
            "https://localhost/",
            "https://127.0.0.1/",
            "https://[::1]/",
            "https://api.example.invalid/",
            "https://api.example.test/",
            "https://example.com/",
            "https://aixionlab.com",
            "https://user@aixionlab.com/",
            "https://aixionlab.com/?next=x",
            "https://aixionlab.com/#fragment",
            "https://aixionlab.com:70000/",
        )
        accepted.forEach { url ->
            val problem = releaseApiUrlProblem(url)
            check(problem == null) { "Release URL guard unexpectedly rejected $url: $problem" }
        }
        rejected.forEach { url ->
            check(releaseApiUrlProblem(url) != null) {
                "Release URL guard unexpectedly accepted unsafe URL: $url"
            }
        }
        logger.lifecycle(
            "Release API URL guard passed: ${accepted.size} accepted and ${rejected.size} rejected cases."
        )
    }
}

tasks.register("verifyReleaseFirebaseConfigGuard") {
    group = "verification"
    description = "Checks release Firebase configuration requirements without loading credentials."
    doLast {
        val accepted = listOf(true to true)
        val rejected = listOf(false to false, false to true, true to false)
        accepted.forEach { (enabled, exists) ->
            val problem = releaseFirebaseConfigProblem(enabled, exists)
            check(problem == null) {
                "Release Firebase guard unexpectedly rejected enabled=$enabled exists=$exists: $problem"
            }
        }
        rejected.forEach { (enabled, exists) ->
            check(releaseFirebaseConfigProblem(enabled, exists) != null) {
                "Release Firebase guard unexpectedly accepted enabled=$enabled exists=$exists."
            }
        }
        logger.lifecycle(
            "Release Firebase config guard passed: ${accepted.size} accepted and ${rejected.size} rejected cases."
        )
    }
}

tasks.matching { it.name == "preReleaseBuild" }.configureEach {
    doFirst {
        val rawUrl = configuredApiBaseUrl
            ?: throw GradleException(
                "Release builds require -PAIXION_API_BASE_URL=https://<production-api-host>/"
            )
        val problem = releaseApiUrlProblem(rawUrl)
        if (problem != null) throw GradleException(problem)
        val firebaseProblem = releaseFirebaseConfigProblem(
            firebaseConfigEnabled,
            firebaseConfigFile.isFile,
        )
        if (firebaseProblem != null) throw GradleException(firebaseProblem)
    }
}

dependencies {
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.compose.ui:ui:1.7.5")
    implementation("androidx.compose.ui:ui-tooling-preview:1.7.5")
    implementation("androidx.compose.material3:material3:1.3.1")
    implementation("androidx.navigation:navigation-compose:2.8.4")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("androidx.work:work-runtime-ktx:2.10.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.squareup.retrofit2:retrofit:2.11.0")
    implementation("com.squareup.retrofit2:converter-gson:2.11.0")
    implementation("com.google.code.gson:gson:2.11.0")
    implementation("com.google.firebase:firebase-messaging:24.1.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-play-services:1.8.1")
    debugImplementation("androidx.compose.ui:ui-tooling:1.7.5")
    debugImplementation("androidx.compose.ui:ui-test-manifest:1.7.5")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.7.0")
    androidTestImplementation("androidx.compose.ui:ui-test-junit4:1.7.5")
}
