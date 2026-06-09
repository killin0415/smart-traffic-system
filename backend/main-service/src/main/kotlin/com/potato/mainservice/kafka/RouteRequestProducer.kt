package com.potato.mainservice.kafka

import com.fasterxml.jackson.databind.ObjectMapper
import org.springframework.kafka.core.KafkaTemplate
import org.springframework.stereotype.Component

@Component
class RouteRequestProducer(
    private val kafkaTemplate: KafkaTemplate<String, String>,
    private val objectMapper: ObjectMapper,
) {

    companion object {
        const val TOPIC = "route.request"
    }

    fun send(
        correlationId: String,
        originLat: Double,
        originLng: Double,
        destLat: Double,
        destLng: Double,
        topK: Int,
    ) {
        val payload = objectMapper.writeValueAsString(
            mapOf(
                "correlation_id" to correlationId,
                "origin_lat" to originLat,
                "origin_lng" to originLng,
                "dest_lat" to destLat,
                "dest_lng" to destLng,
                "top_k" to topK,
            )
        )
        kafkaTemplate.send(TOPIC, correlationId, payload)
    }
}
