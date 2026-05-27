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
            header { string("Content-Type", org.hamcrest.Matchers.containsString("charset=UTF-8")) }
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
            header { string("Content-Type", org.hamcrest.Matchers.containsString("charset=UTF-8")) }
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

        inOrder(pendingRequestStore, chatRequestProducer) {
            verify(pendingRequestStore).register(any())
            verify(chatRequestProducer).send(any(), any(), any())
        }
    }

    @Test
    fun `POST message should deserialize snake_case route_payload into routeResult with @JsonAlias`() {
        whenever(pendingRequestStore.await(any(), eq(30))).thenReturn(
            """{
              "reply":"已為您規劃路線",
              "suggested_actions":[],
              "route_payload":{
                "routes":[
                  {
                    "path":[1,2,3],
                    "edges":[10,20],
                    "road_names":["中山北路","民權西路"],
                    "estimated_time_min":15.5,
                    "distance_km":4.2,
                    "speed_cameras":[
                      {"latitude":25.05,"longitude":121.52,"direction":"N","speed_limit":50,"address":"中山北路"}
                    ],
                    "parking_suggestions":[
                      {"id":1,"name":"P1","address":"X","available_car":12,"distance_m":150.0}
                    ]
                  }
                ]
              }
            }""".trimIndent()
        )

        mockMvc.post("/api/v1/chat/message") {
            contentType = MediaType.APPLICATION_JSON
            content = """{"session_id":"s1","content":"我要從台北車站到忠孝復興"}"""
        }.andExpect {
            status { isOk() }
            header { string("Content-Type", org.hamcrest.Matchers.containsString("charset=UTF-8")) }
            jsonPath("$.reply") { value("已為您規劃路線") }
            // Verify @JsonAlias actually wired the snake_case fields into camelCase Kotlin properties
            jsonPath("$.routeResult.routes[0].estimatedTimeMin") { value(15.5) }
            jsonPath("$.routeResult.routes[0].distanceKm") { value(4.2) }
            jsonPath("$.routeResult.routes[0].roadNames[0]") { value("中山北路") }
            jsonPath("$.routeResult.routes[0].speedCameras[0].speedLimit") { value(50) }
            jsonPath("$.routeResult.routes[0].parkingSuggestions[0].availableCar") { value(12) }
            jsonPath("$.routeResult.routes[0].parkingSuggestions[0].distanceM") { value(150.0) }
        }
    }

    @Test
    fun `POST message should set routeResult to null when route_payload is malformed`() {
        whenever(pendingRequestStore.await(any(), eq(30))).thenReturn(
            """{
              "reply":"路線好像怪怪的",
              "suggested_actions":[],
              "route_payload": "this is not an object"
            }""".trimIndent()
        )

        mockMvc.post("/api/v1/chat/message") {
            contentType = MediaType.APPLICATION_JSON
            content = """{"session_id":"s1","content":"hi"}"""
        }.andExpect {
            status { isOk() }
            jsonPath("$.reply") { value("路線好像怪怪的") }
            jsonPath("$.routeResult") { value(org.hamcrest.Matchers.nullValue()) }
        }
    }

    @Test
    fun `POST message should set routeResult to null when route_payload absent`() {
        whenever(pendingRequestStore.await(any(), eq(30))).thenReturn(
            """{"reply":"沒有路線意圖","suggested_actions":[]}"""
        )

        mockMvc.post("/api/v1/chat/message") {
            contentType = MediaType.APPLICATION_JSON
            content = """{"session_id":"s1","content":"hello"}"""
        }.andExpect {
            status { isOk() }
            jsonPath("$.reply") { value("沒有路線意圖") }
            jsonPath("$.routeResult") { value(org.hamcrest.Matchers.nullValue()) }
        }
    }
}
