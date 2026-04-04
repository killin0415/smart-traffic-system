package com.potato.mainservice.kafka

import org.apache.kafka.clients.consumer.ConsumerRecord
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.mockito.kotlin.*

class ChatResponseConsumerTest {

    private lateinit var pendingRequestStore: PendingRequestStore
    private lateinit var consumer: ChatResponseConsumer

    @BeforeEach
    fun setUp() {
        pendingRequestStore = mock()
        consumer = ChatResponseConsumer(pendingRequestStore)
    }

    @Test
    fun `onChatResponse should complete pending request with correct correlationId`() {
        val record = ConsumerRecord<String, String>(
            "chat.response", 0, 0L, "corr-123", """{"reply":"hi"}"""
        )

        consumer.onChatResponse(record)

        verify(pendingRequestStore).complete("corr-123", """{"reply":"hi"}""")
    }

    @Test
    fun `onChatResponse should not call complete when key is null`() {
        val record = ConsumerRecord<String, String>(
            "chat.response", 0, 0L, null, """{"reply":"hi"}"""
        )

        consumer.onChatResponse(record)

        verify(pendingRequestStore, never()).complete(any(), any())
    }
}
