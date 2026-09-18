package com.aixion.controltower.core.model

import com.aixion.controltower.core.api.dto.TrustExceptionDto

/**
 * Gson may materialize a Kotlin non-null collection field as null when an API payload
 * explicitly contains null. Keep native-action rendering resilient so a malformed or
 * legacy exception payload cannot crash the approval surface.
 */
val TrustExceptionDto.safePaths: List<String>
    get() = paths.orEmpty()

val TrustExceptionDto.safeNetworkDomains: List<String>
    get() = networkDomains.orEmpty()
