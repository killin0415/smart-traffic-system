package com.potato.mainservice.domain

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
)
