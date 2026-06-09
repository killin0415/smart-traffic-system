package com.potato.mainservice.kafka

import com.fasterxml.jackson.databind.ObjectMapper
import org.springframework.kafka.core.KafkaTemplate
import org.springframework.stereotype.Component

@Component
class GeocodeRequestProducer(
    private val kafkaTemplate: KafkaTemplate<String, String>,
    private val objectMapper: ObjectMapper,
) {

    companion object {
        const val TOPIC = "geocode.request"
    }

    fun send(
        correlationId: String,
        query: String,
        cityHint: String?,
        limit: Int,
    ) {
        val payloadMap = mutableMapOf<String, Any>(
            "correlation_id" to correlationId,
            "query" to query,
            "limit" to limit,
        )
        if (!cityHint.isNullOrBlank()) {
            payloadMap["city_hint"] = cityHint
        }
        val payload = objectMapper.writeValueAsString(payloadMap)
        kafkaTemplate.send(TOPIC, correlationId, payload)
    }
}
