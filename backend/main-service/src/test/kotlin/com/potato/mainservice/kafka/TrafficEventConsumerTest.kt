package com.potato.mainservice.kafka

import org.junit.jupiter.api.Test
import org.junit.jupiter.api.assertDoesNotThrow

class TrafficEventConsumerTest {

    private val consumer = TrafficEventConsumer()

    @Test
    fun `onTrafficAlert should handle message without throwing`() {
        assertDoesNotThrow {
            consumer.onTrafficAlert("""{"alert":"congestion","road":"中山路"}""")
        }
    }

    @Test
    fun `onRouteResult should handle message without throwing`() {
        assertDoesNotThrow {
            consumer.onRouteResult("""{"route_id":"r1","path":"A->B","estimated_time":10}""")
        }
    }

    @Test
    fun `onTrafficAlert should handle empty message`() {
        assertDoesNotThrow {
            consumer.onTrafficAlert("")
        }
    }

    @Test
    fun `onRouteResult should handle empty message`() {
        assertDoesNotThrow {
            consumer.onRouteResult("")
        }
    }
}
