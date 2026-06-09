package com.potato.mainservice.kafka

import org.apache.kafka.clients.consumer.ConsumerRecord
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.assertDoesNotThrow
import org.mockito.kotlin.*

class GeocodeResponseConsumerTest {

    private lateinit var pendingRequestStore: PendingRequestStore
    private lateinit var consumer: GeocodeResponseConsumer

    @BeforeEach
    fun setUp() {
        pendingRequestStore = mock()
        consumer = GeocodeResponseConsumer(pendingRequestStore)
    }

    @Test
    fun `onGeocodeResponse should complete pending request with correct correlationId`() {
        val record = ConsumerRecord<String, String>(
            "geocode.response", 0, 0L, "corr-1", """{"correlation_id":"corr-1","results":[]}"""
        )

        consumer.onGeocodeResponse(record)

        verify(pendingRequestStore).complete("corr-1", """{"correlation_id":"corr-1","results":[]}""")
    }

    @Test
    fun `onGeocodeResponse should not throw when key is unknown to store`() {
        whenever(pendingRequestStore.complete(any(), any())).thenReturn(false)

        val record = ConsumerRecord<String, String>(
            "geocode.response", 0, 0L, "unknown-key", """{"results":[]}"""
        )

        assertDoesNotThrow { consumer.onGeocodeResponse(record) }
    }

    @Test
    fun `onGeocodeResponse should not call complete when key is null`() {
        val record = ConsumerRecord<String, String>(
            "geocode.response", 0, 0L, null, """{"results":[]}"""
        )

        consumer.onGeocodeResponse(record)

        verify(pendingRequestStore, never()).complete(any(), any())
    }
}
