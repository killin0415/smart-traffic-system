package com.potato.mainservice.kafka

import org.junit.jupiter.api.Assertions.*
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import java.util.concurrent.TimeoutException

class PendingRequestStoreTest {

    private lateinit var store: PendingRequestStore

    @BeforeEach
    fun setUp() {
        store = PendingRequestStore()
    }

    @Test
    fun `register should create a pending future`() {
        val future = store.register("corr-1")
        assertNotNull(future)
        assertFalse(future.isDone)
    }

    @Test
    fun `complete should resolve the pending future and return true`() {
        store.register("corr-1")
        val result = store.complete("corr-1", """{"reply":"hello"}""")
        assertTrue(result)
    }

    @Test
    fun `complete with unknown correlationId should return false`() {
        val result = store.complete("unknown-id", """{"reply":"hello"}""")
        assertFalse(result)
    }

    @Test
    fun `await should return the response after complete`() {
        store.register("corr-1")

        // Complete in another thread
        Thread {
            Thread.sleep(50)
            store.complete("corr-1", """{"reply":"world"}""")
        }.start()

        val response = store.await("corr-1", 5)
        assertEquals("""{"reply":"world"}""", response)
    }

    @Test
    fun `await should throw TimeoutException when no response within timeout`() {
        store.register("corr-1")
        assertThrows(TimeoutException::class.java) {
            store.await("corr-1", 1)
        }
    }

    @Test
    fun `await should throw IllegalStateException for unregistered correlationId`() {
        assertThrows(IllegalStateException::class.java) {
            store.await("non-existent", 1)
        }
    }

    @Test
    fun `complete should remove the future from pending map`() {
        store.register("corr-1")
        store.complete("corr-1", "response")
        // Second complete should return false since it's already removed
        assertFalse(store.complete("corr-1", "response2"))
    }
}
