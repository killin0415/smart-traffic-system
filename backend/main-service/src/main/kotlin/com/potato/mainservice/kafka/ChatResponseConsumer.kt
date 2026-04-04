package com.potato.mainservice.kafka

import org.apache.kafka.clients.consumer.ConsumerRecord
import org.slf4j.LoggerFactory
import org.springframework.kafka.annotation.KafkaListener
import org.springframework.stereotype.Component

@Component
class ChatResponseConsumer(
    private val pendingRequestStore: PendingRequestStore,
) {

    private val log = LoggerFactory.getLogger(ChatResponseConsumer::class.java)

    @KafkaListener(topics = ["chat.response"], groupId = "main-service-group")
    fun onChatResponse(record: ConsumerRecord<String, String>) {
        log.info("[ChatResponseConsumer] Received: key={}, value={}", record.key(), record.value())

        val correlationId = record.key()
        if (correlationId == null) {
            log.warn("[ChatResponseConsumer] Record key is null, cannot correlate response")
            return
        }

        val completed = pendingRequestStore.complete(correlationId, record.value())
        log.info("[ChatResponseConsumer] correlationId={}, matched={}", correlationId, completed)
    }
}
