package com.potato.mainservice.controller

import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.RestController

@RestController
class HealthController {

    data class HealthStatus(
        val main_service: String,
    )

    @GetMapping("/health")
    fun healthCheck(): HealthStatus {
        return HealthStatus(
            main_service = "SERVING",
        )
    }
}
