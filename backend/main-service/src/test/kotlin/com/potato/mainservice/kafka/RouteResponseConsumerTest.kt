package com.potato.mainservice.kafka

import org.apache.kafka.clients.consumer.ConsumerRecord
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.assertDoesNotThrow
import org.mockito.kotlin.*

class RouteResponseConsumerTest {

    private lateinit var pendingRequestStore: PendingRequestStore
    private lateinit var consumer: RouteResponseConsumer

    @BeforeEach
    fun setUp() {
        pendingRequestStore = mock()
        consumer = RouteResponseConsumer(pendingRequestStore)
    }

    @Test
    fun `onRouteResponse should complete pending request with correct correlationId`() {
        val record = ConsumerRecord<String, String>(
            "route.response", 0, 0L, "corr-1", """{"correlation_id":"corr-1","routes":[]}"""
        )

        consumer.onRouteResponse(record)

        verify(pendingRequestStore).complete("corr-1", """{"correlation_id":"corr-1","routes":[]}""")
    }

    @Test
    fun `onRouteResponse should not throw when key is not in store`() {
        whenever(pendingRequestStore.complete(any(), any())).thenReturn(false)

        val record = ConsumerRecord<String, String>(
            "route.response", 0, 0L, "unknown-key", """{"routes":[]}"""
        )

        assertDoesNotThrow { consumer.onRouteResponse(record) }
    }

    @Test
    fun `onRouteResponse should not call complete when key is null`() {
        val record = ConsumerRecord<String, String>(
            "route.response", 0, 0L, null, """{"routes":[]}"""
        )

        consumer.onRouteResponse(record)

        verify(pendingRequestStore, never()).complete(any(), any())
    }
}
