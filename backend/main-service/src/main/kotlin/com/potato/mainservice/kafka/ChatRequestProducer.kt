package com.potato.mainservice.kafka

import com.fasterxml.jackson.databind.ObjectMapper
import org.springframework.kafka.core.KafkaTemplate
import org.springframework.stereotype.Component

@Component
class ChatRequestProducer(
    private val kafkaTemplate: KafkaTemplate<String, String>,
    private val objectMapper: ObjectMapper,
) {

    companion object {
        const val TOPIC = "chat.request"
    }

    fun send(correlationId: String, sessionId: String, content: String) {
        val payload = objectMapper.writeValueAsString(
            mapOf(
                "correlation_id" to correlationId,
                "session_id" to sessionId,
                "content" to content,
            )
        )
        kafkaTemplate.send(TOPIC, correlationId, payload)
    }
}
