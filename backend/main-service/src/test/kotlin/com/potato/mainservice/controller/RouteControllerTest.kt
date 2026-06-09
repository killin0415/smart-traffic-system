package com.potato.mainservice.controller

import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import com.potato.mainservice.kafka.PendingRequestStore
import com.potato.mainservice.kafka.RouteRequestProducer
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.mockito.kotlin.*
import org.springframework.http.MediaType
import org.springframework.test.web.servlet.MockMvc
import org.springframework.test.web.servlet.post
import org.springframework.test.web.servlet.setup.MockMvcBuilders
import java.util.concurrent.TimeoutException

class RouteControllerTest {

    private lateinit var mockMvc: MockMvc
    private lateinit var routeRequestProducer: RouteRequestProducer
    private lateinit var pendingRequestStore: PendingRequestStore

    @BeforeEach
    fun setUp() {
        routeRequestProducer = mock()
        pendingRequestStore = mock()
        val objectMapper = jacksonObjectMapper()
        val controller = RouteController(routeRequestProducer, pendingRequestStore, objectMapper)
        mockMvc = MockMvcBuilders.standaloneSetup(controller).build()
    }

    @Test
    fun `POST route should return 200 with routes when agent responds`() {
        whenever(pendingRequestStore.await(any(), eq(30))).thenReturn(
            """{
              "correlation_id":"c1",
              "routes":[
                {"path":[1,2,3],"edges":[10,20],"road_names":["中山路"],
                 "estimated_time_min":12.5,"distance_km":3.4,
                 "speed_cameras":[],"parking_suggestions":[]}
              ]
            }""".trimIndent()
        )

        mockMvc.post("/api/v1/route") {
            contentType = MediaType.APPLICATION_JSON
            content = """{"originLat":25.0478,"originLng":121.5170,"destLat":25.0418,"destLng":121.5654,"topK":3}"""
        }.andExpect {
            status { isOk() }
            header { string("Content-Type", org.hamcrest.Matchers.containsString("charset=UTF-8")) }
            jsonPath("$.routes[0].estimatedTimeMin") { value(12.5) }
            jsonPath("$.routes[0].distanceKm") { value(3.4) }
            jsonPath("$.routes[0].roadNames[0]") { value("中山路") }
        }

        verify(pendingRequestStore).register(any())
        verify(routeRequestProducer).send(any(), eq(25.0478), eq(121.5170), eq(25.0418), eq(121.5654), eq(3))
    }

    @Test
    fun `POST route should return 504 when agent times out`() {
        whenever(pendingRequestStore.await(any(), any())).thenAnswer { throw TimeoutException("timeout") }

        mockMvc.post("/api/v1/route") {
            contentType = MediaType.APPLICATION_JSON
            content = """{"originLat":25.0,"originLng":121.5,"destLat":25.1,"destLng":121.6}"""
        }.andExpect {
            status { isGatewayTimeout() }
            header { string("Content-Type", org.hamcrest.Matchers.containsString("charset=UTF-8")) }
            jsonPath("$.error") { exists() }
        }
    }

    @Test
    fun `POST route should return 400 when originLat missing`() {
        mockMvc.post("/api/v1/route") {
            contentType = MediaType.APPLICATION_JSON
            content = """{"originLng":121.5,"destLat":25.1,"destLng":121.6}"""
        }.andExpect {
            status { isBadRequest() }
            header { string("Content-Type", org.hamcrest.Matchers.containsString("charset=UTF-8")) }
            jsonPath("$.error") { value("originLat is required") }
        }

        verify(routeRequestProducer, never()).send(any(), any(), any(), any(), any(), any())
    }

    @Test
    fun `POST route should return 400 when destLng missing`() {
        mockMvc.post("/api/v1/route") {
            contentType = MediaType.APPLICATION_JSON
            content = """{"originLat":25.0,"originLng":121.5,"destLat":25.1}"""
        }.andExpect {
            status { isBadRequest() }
            header { string("Content-Type", org.hamcrest.Matchers.containsString("charset=UTF-8")) }
            jsonPath("$.error") { value("destLng is required") }
        }
    }

    @Test
    fun `POST route should default topK to 3 when missing`() {
        whenever(pendingRequestStore.await(any(), eq(30))).thenReturn(
            """{"correlation_id":"c1","routes":[]}"""
        )

        mockMvc.post("/api/v1/route") {
            contentType = MediaType.APPLICATION_JSON
            content = """{"originLat":25.0,"originLng":121.5,"destLat":25.1,"destLng":121.6}"""
        }.andExpect {
            status { isOk() }
        }

        verify(routeRequestProducer).send(any(), any(), any(), any(), any(), eq(3))
    }
}
