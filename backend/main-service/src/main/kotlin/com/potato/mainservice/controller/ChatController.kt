package com.potato.mainservice.controller

import com.fasterxml.jackson.databind.ObjectMapper
import com.potato.mainservice.domain.ChatMessageRequest
import com.potato.mainservice.domain.ChatMessageResponse
import com.potato.mainservice.kafka.ChatRequestProducer
import com.potato.mainservice.kafka.PendingRequestStore
import org.springframework.http.HttpStatus
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.*
import java.util.UUID
import java.util.concurrent.TimeoutException

@RestController
@RequestMapping("/api/v1/chat")
class ChatController(
    private val chatRequestProducer: ChatRequestProducer,
    private val pendingRequestStore: PendingRequestStore,
    private val objectMapper: ObjectMapper,
) {

    @PostMapping("/message", produces = ["application/json;charset=UTF-8"])
    fun sendMessage(@RequestBody request: ChatMessageRequest): ResponseEntity<Any> {
        val correlationId = UUID.randomUUID().toString()

        pendingRequestStore.register(correlationId)
        chatRequestProducer.send(correlationId, request.session_id, request.content)

        return try {
            val responseJson: String = pendingRequestStore.await(correlationId, 30)
            val responseMap = objectMapper.readTree(responseJson)

            val response = ChatMessageResponse(
                reply = responseMap["reply"]?.asText() ?: "",
                suggested_actions = responseMap["suggested_actions"]
                    ?.map { it.asText() } ?: emptyList(),
            )
            ResponseEntity.ok(response)
        } catch (e: TimeoutException) {
            ResponseEntity.status(HttpStatus.GATEWAY_TIMEOUT)
                .body(mapOf("error" to "Multiagent service did not respond within 30 seconds"))
        }
    }
}
