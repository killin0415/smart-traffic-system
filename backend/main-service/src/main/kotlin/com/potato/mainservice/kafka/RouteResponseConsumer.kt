package com.potato.mainservice.kafka

import org.apache.kafka.clients.consumer.ConsumerRecord
import org.slf4j.LoggerFactory
import org.springframework.kafka.annotation.KafkaListener
import org.springframework.stereotype.Component

@Component
class RouteResponseConsumer(
    private val pendingRequestStore: PendingRequestStore,
) {

    private val log = LoggerFactory.getLogger(RouteResponseConsumer::class.java)

    @KafkaListener(topics = ["route.response"], groupId = "main-service-group")
    fun onRouteResponse(record: ConsumerRecord<String, String>) {
        log.info("[RouteResponseConsumer] Received: key={}, value={}", record.key(), record.value())

        val correlationId = record.key()
        if (correlationId == null) {
            log.warn("[RouteResponseConsumer] Record key is null, cannot correlate response")
            return
        }

        val completed = pendingRequestStore.complete(correlationId, record.value())
        if (!completed) {
            log.info(
                "[RouteResponseConsumer] No pending request for correlationId={} — likely already timed out, dropping",
                correlationId,
            )
        }
    }
}
