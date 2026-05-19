package com.potato.mainservice.domain

import com.potato.mainservice.kafka.RouteResponse

/**
 * Request/Response data classes for the Chat API.
 */

data class ChatMessageRequest(
    val session_id: String,
    val content: String,
)

data class ChatMessageResponse(
    val reply: String,
    val suggested_actions: List<String>,
    val routeResult: RouteResponse? = null,
)
