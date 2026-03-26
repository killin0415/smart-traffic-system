package com.potato.mainservice.controller

import com.potato.mainservice.domain.ChatMessageRequest
import com.potato.mainservice.domain.ChatMessageResponse
import com.potato.mainservice.grpc.ChatServiceGrpc
import com.potato.mainservice.grpc.ChatRequest
import org.springframework.web.bind.annotation.*

/**
 * REST controller that bridges HTTP requests to the agent-service via gRPC.
 */
@RestController
@RequestMapping("/api/v1/chat")
class ChatController(
    private val chatStub: ChatServiceGrpc.ChatServiceBlockingStub,
) {

    /**
     * POST /api/v1/chat/message
     * Forwards user message to agent-service via gRPC and returns the AI response.
     */
    @PostMapping("/message", produces = ["application/json;charset=UTF-8"])
    fun sendMessage(@RequestBody request: ChatMessageRequest): ChatMessageResponse {
        val grpcRequest = ChatRequest.newBuilder()
            .setSessionId(request.session_id)
            .setContent(request.content)
            .build()

        val grpcResponse = chatStub.sendMessage(grpcRequest)

        return ChatMessageResponse(
            reply = grpcResponse.reply,
            suggested_actions = grpcResponse.suggestedActionsList,
        )
    }
}
