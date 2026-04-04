package com.potato.mainservice.controller

import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import com.potato.mainservice.kafka.ChatRequestProducer
import com.potato.mainservice.kafka.PendingRequestStore
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.mockito.kotlin.*
import org.springframework.http.MediaType
import org.springframework.test.web.servlet.MockMvc
import org.springframework.test.web.servlet.post
import org.springframework.test.web.servlet.setup.MockMvcBuilders
import java.util.concurrent.TimeoutException

class ChatControllerTest {

    private lateinit var mockMvc: MockMvc
    private lateinit var chatRequestProducer: ChatRequestProducer
    private lateinit var pendingRequestStore: PendingRequestStore

    @BeforeEach
    fun setUp() {
        chatRequestProducer = mock()
        pendingRequestStore = mock()
        val objectMapper = jacksonObjectMapper()
        val controller = ChatController(chatRequestProducer, pendingRequestStore, objectMapper)
        mockMvc = MockMvcBuilders.standaloneSetup(controller).build()
    }

    @Test
    fun `POST message should return 200 with reply when agent responds`() {
        whenever(pendingRequestStore.await(any(), eq(30))).thenReturn(
            """{"reply":"你好！","suggested_actions":["查看路況"]}"""
        )

        mockMvc.post("/api/v1/chat/message") {
            contentType = MediaType.APPLICATION_JSON
            content = """{"session_id":"s1","content":"hello"}"""
        }.andExpect {
            status { isOk() }
            jsonPath("$.reply") { value("你好！") }
            jsonPath("$.suggested_actions[0]") { value("查看路況") }
        }

        verify(pendingRequestStore).register(any())
        verify(chatRequestProducer).send(any(), eq("s1"), eq("hello"))
    }

    @Test
    fun `POST message should return 504 when agent times out`() {
        whenever(pendingRequestStore.await(any(), any())).thenAnswer { throw TimeoutException("timeout") }

        mockMvc.post("/api/v1/chat/message") {
            contentType = MediaType.APPLICATION_JSON
            content = """{"session_id":"s1","content":"hello"}"""
        }.andExpect {
            status { isGatewayTimeout() }
            jsonPath("$.error") { exists() }
        }
    }

    @Test
    fun `POST message should register correlationId before sending`() {
        whenever(pendingRequestStore.await(any(), eq(30))).thenReturn(
            """{"reply":"ok","suggested_actions":[]}"""
        )

        mockMvc.post("/api/v1/chat/message") {
            contentType = MediaType.APPLICATION_JSON
            content = """{"session_id":"s1","content":"test"}"""
        }

        // register should be called before send
        inOrder(pendingRequestStore, chatRequestProducer) {
            verify(pendingRequestStore).register(any())
            verify(chatRequestProducer).send(any(), any(), any())
        }
    }
}
