package com.potato.mainservice.kafka

import org.apache.kafka.clients.consumer.ConsumerRecord
import org.slf4j.LoggerFactory
import org.springframework.kafka.annotation.KafkaListener
import org.springframework.stereotype.Component

@Component
class GeocodeResponseConsumer(
    private val pendingRequestStore: PendingRequestStore,
) {

    private val log = LoggerFactory.getLogger(GeocodeResponseConsumer::class.java)

    @KafkaListener(topics = ["geocode.response"], groupId = "main-service-group")
    fun onGeocodeResponse(record: ConsumerRecord<String, String>) {
        log.info("[GeocodeResponseConsumer] Received: key={}, value={}", record.key(), record.value())

        val correlationId = record.key()
        if (correlationId == null) {
            log.warn("[GeocodeResponseConsumer] Record key is null, cannot correlate response")
            return
        }

        val completed = pendingRequestStore.complete(correlationId, record.value())
        if (!completed) {
            log.info(
                "[GeocodeResponseConsumer] No pending request for correlationId={} — likely already timed out, dropping",
                correlationId,
            )
        }
    }
}
