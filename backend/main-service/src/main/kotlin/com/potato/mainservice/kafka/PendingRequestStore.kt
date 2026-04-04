package com.potato.mainservice.kafka

import org.slf4j.LoggerFactory
import org.springframework.stereotype.Component
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException

@Component
class PendingRequestStore {

    private val log = LoggerFactory.getLogger(PendingRequestStore::class.java)
    private val pending = ConcurrentHashMap<String, CompletableFuture<String>>()

    fun register(correlationId: String): CompletableFuture<String> {
        val future = CompletableFuture<String>()
        pending[correlationId] = future
        log.info("[PendingRequestStore] Registered correlationId={}, pending size={}", correlationId, pending.size)
        return future
    }

    fun complete(correlationId: String, response: String): Boolean {
        val future = pending.remove(correlationId)
        return if (future != null) {
            future.complete(response)
            true
        } else {
            false
        }
    }

    fun await(correlationId: String, timeoutSeconds: Long = 30): String {
        val future = pending[correlationId]
            ?: throw IllegalStateException("No pending request for correlationId: $correlationId")
        return try {
            future.get(timeoutSeconds, TimeUnit.SECONDS)
        } catch (e: TimeoutException) {
            pending.remove(correlationId)
            throw e
        }
    }
}
