package com.potato.mainservice.controller

import com.fasterxml.jackson.databind.ObjectMapper
import com.potato.mainservice.domain.RouteRequest
import com.potato.mainservice.kafka.PendingRequestStore
import com.potato.mainservice.kafka.RouteRequestProducer
import com.potato.mainservice.kafka.RouteResponse
import org.springframework.http.HttpStatus
import org.springframework.http.MediaType
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.*
import java.util.UUID
import java.util.concurrent.TimeoutException

private val JSON_UTF8: MediaType = MediaType.parseMediaType("application/json;charset=UTF-8")

@RestController
@RequestMapping("/api/v1/route")
class RouteController(
    private val routeRequestProducer: RouteRequestProducer,
    private val pendingRequestStore: PendingRequestStore,
    private val objectMapper: ObjectMapper,
) {

    @PostMapping(produces = ["application/json;charset=UTF-8"])
    fun planRoute(@RequestBody request: RouteRequest): ResponseEntity<Any> {
        val missing = when {
            request.originLat == null -> "originLat"
            request.originLng == null -> "originLng"
            request.destLat == null -> "destLat"
            request.destLng == null -> "destLng"
            else -> null
        }
        if (missing != null) {
            return ResponseEntity.status(HttpStatus.BAD_REQUEST)
                .contentType(JSON_UTF8)
                .body(mapOf("error" to "$missing is required"))
        }

        val correlationId = UUID.randomUUID().toString()
        val topK = request.topK ?: 3

        pendingRequestStore.register(correlationId)
        routeRequestProducer.send(
            correlationId,
            request.originLat!!,
            request.originLng!!,
            request.destLat!!,
            request.destLng!!,
            topK,
        )

        return try {
            val responseJson: String = pendingRequestStore.await(correlationId, 30)
            val response = objectMapper.readValue(responseJson, RouteResponse::class.java)
            ResponseEntity.ok(response)
        } catch (e: TimeoutException) {
            ResponseEntity.status(HttpStatus.GATEWAY_TIMEOUT)
                .contentType(JSON_UTF8)
                .body(mapOf("error" to "Multiagent service did not respond within 30 seconds"))
        }
    }
}
