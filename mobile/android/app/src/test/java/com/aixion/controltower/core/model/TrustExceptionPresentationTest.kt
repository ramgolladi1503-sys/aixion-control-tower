package com.aixion.controltower.core.model

import com.aixion.controltower.core.api.dto.TrustExceptionDto
import com.google.gson.Gson
import org.junit.Assert.assertTrue
import org.junit.Test

class TrustExceptionPresentationTest {
    @Test
    fun null_native_action_collections_from_json_are_normalized_to_empty_lists() {
        val dto = Gson().fromJson(
            """
            {
              "id": "action:test",
              "category": "REQUIRE_APPROVAL",
              "severity": "MEDIUM",
              "title": "Agent action needs exact approval",
              "summary": "test",
              "paths": null,
              "network_domains": null
            }
            """.trimIndent(),
            TrustExceptionDto::class.java
        )

        assertTrue(dto.safePaths.isEmpty())
        assertTrue(dto.safeNetworkDomains.isEmpty())
    }
}
