package com.potato.mainservice.kafka

import org.springframework.kafka.annotation.KafkaListener
import org.springframework.stereotype.Component

/**
 * Kafka consumer for traffic alert events (congestion / incident notifications).
 * `route.response` is handled by [RouteResponseConsumer] which bridges back to
 * `PendingRequestStore` — having both listeners on the same topic + groupId
 * would cause partition contention.
 */
@Component
class TrafficEventConsumer {

    @KafkaListener(topics = ["traffic.alerts"], groupId = "main-service-group")
    fun onTrafficAlert(message: String) {
        println("[Kafka Consumer] Received traffic alert: $message")
        // TODO: Push to Mobile client via WebSocket or Push Notification
    }
}
