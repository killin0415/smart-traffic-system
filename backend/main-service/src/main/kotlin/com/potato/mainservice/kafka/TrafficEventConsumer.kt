package com.potato.mainservice.kafka

import org.springframework.kafka.annotation.KafkaListener
import org.springframework.stereotype.Component

/**
 * Kafka consumer for main-service.
 * Listens for traffic congestion alerts and agent processing results
 * to push WebSocket/Push Notifications to the Mobile client.
 */
@Component
class TrafficEventConsumer {

    /**
     * Listens for processed traffic data (e.g., congestion alerts from agent-service).
     */
    @KafkaListener(topics = ["traffic.alerts"], groupId = "main-service-group")
    fun onTrafficAlert(message: String) {
        println("[Kafka Consumer] Received traffic alert: $message")
        // TODO: Push to Mobile client via WebSocket or Push Notification
    }

    @KafkaListener(topics = ["route.response"], groupId = "main-service-group")
    fun onRouteResult(message: String) {
        println("[Kafka Consumer] Received route result: $message")
        // TODO: Cache in Redis or forward to requesting client
    }
}
