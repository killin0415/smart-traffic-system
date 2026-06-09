package com.potato.mainservice.kafka

import com.fasterxml.jackson.databind.ObjectMapper
import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.mockito.kotlin.*
import org.springframework.kafka.core.KafkaTemplate

class ChatRequestProducerTest {

    private lateinit var kafkaTemplate: KafkaTemplate<String, String>
    private lateinit var objectMapper: ObjectMapper
    private lateinit var producer: ChatRequestProducer

    @BeforeEach
    fun setUp() {
        kafkaTemplate = mock()
        objectMapper = jacksonObjectMapper()
        producer = ChatRequestProducer(kafkaTemplate, objectMapper)
    }

    @Test
    fun `send should publish message to chat_request topic with correct key`() {
        producer.send("corr-123", "session-1", "Hello")

        verify(kafkaTemplate).send(
            eq("chat.request"),
            eq("corr-123"),
            argThat { payload ->
                val tree = objectMapper.readTree(payload)
                tree["correlation_id"].asText() == "corr-123" &&
                        tree["session_id"].asText() == "session-1" &&
                        tree["content"].asText() == "Hello"
            }
        )
    }

    @Test
    fun `send should serialize payload as JSON with all fields`() {
        producer.send("id-1", "sess-2", "你好")

        verify(kafkaTemplate).send(
            eq("chat.request"),
            eq("id-1"),
            argThat { payload ->
                val tree = objectMapper.readTree(payload)
                tree.has("correlation_id") &&
                        tree.has("session_id") &&
                        tree.has("content") &&
                        tree["content"].asText() == "你好"
            }
        )
    }
}
