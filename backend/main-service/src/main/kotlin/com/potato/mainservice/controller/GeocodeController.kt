package com.potato.mainservice.controller

import com.fasterxml.jackson.databind.ObjectMapper
import com.potato.mainservice.domain.GeocodeResponse
import com.potato.mainservice.kafka.GeocodeRequestProducer
import com.potato.mainservice.kafka.PendingRequestStore
import org.springframework.http.HttpStatus
import org.springframework.http.MediaType
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.*
import java.util.UUID
import java.util.concurrent.TimeoutException

private val JSON_UTF8: MediaType = MediaType.parseMediaType("application/json;charset=UTF-8")

@RestController
@RequestMapping("/api/v1/geocode")
class GeocodeController(
    private val geocodeRequestProducer: GeocodeRequestProducer,
    private val pendingRequestStore: PendingRequestStore,
    private val objectMapper: ObjectMapper,
) {

    companion object {
        private const val MAX_LIMIT = 10
        private const val MIN_LIMIT = 1
        private const val DEFAULT_LIMIT = 5
    }

    @GetMapping(produces = ["application/json;charset=UTF-8"])
    fun geocode(
        @RequestParam("q", required = false) q: String?,
        @RequestParam("cityHint", required = false) cityHint: String?,
        @RequestParam("limit", required = false) limit: Int?,
    ): ResponseEntity<Any> {
        val query = q?.trim().orEmpty()
        if (query.isEmpty()) {
            return ResponseEntity.status(HttpStatus.BAD_REQUEST)
                .contentType(JSON_UTF8)
                .body(mapOf("error" to "q is required"))
        }

        val effectiveLimit = (limit ?: DEFAULT_LIMIT).coerceIn(MIN_LIMIT, MAX_LIMIT)

        val correlationId = UUID.randomUUID().toString()
        pendingRequestStore.register(correlationId)
        geocodeRequestProducer.send(correlationId, query, cityHint, effectiveLimit)

        return try {
            val responseJson = pendingRequestStore.await(correlationId, 30)
            val response = objectMapper.readValue(responseJson, GeocodeResponse::class.java)
            ResponseEntity.ok(response)
        } catch (e: TimeoutException) {
            ResponseEntity.status(HttpStatus.GATEWAY_TIMEOUT)
                .contentType(JSON_UTF8)
                .body(mapOf("error" to "Multiagent service did not respond within 30 seconds"))
        }
    }
}
