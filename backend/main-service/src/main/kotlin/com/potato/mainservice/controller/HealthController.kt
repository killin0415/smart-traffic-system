package com.potato.mainservice.controller

import com.potato.mainservice.grpc.ChatServiceGrpc
import com.potato.mainservice.grpc.HealthCheckRequest
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.RestController

/**
 * Health check endpoints for main-service and agent-service connectivity.
 */
@RestController
class HealthController(
    private val chatStub: ChatServiceGrpc.ChatServiceBlockingStub,
) {

    data class HealthStatus(
        val main_service: String,
        val agent_service: String,
    )

    @GetMapping("/health")
    fun healthCheck(): HealthStatus {
        val agentStatus = try {
            val response = chatStub.healthCheck(HealthCheckRequest.getDefaultInstance())
            "${response.status} (${response.serviceName})"
        } catch (e: Exception) {
            "UNAVAILABLE: ${e.message}"
        }

        return HealthStatus(
            main_service = "SERVING",
            agent_service = agentStatus,
        )
    }
}
