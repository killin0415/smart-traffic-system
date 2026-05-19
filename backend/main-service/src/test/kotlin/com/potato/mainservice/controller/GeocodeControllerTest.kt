package com.potato.mainservice.controller

import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import com.potato.mainservice.kafka.GeocodeRequestProducer
import com.potato.mainservice.kafka.PendingRequestStore
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.mockito.kotlin.*
import org.springframework.test.web.servlet.MockMvc
import org.springframework.test.web.servlet.get
import org.springframework.test.web.servlet.setup.MockMvcBuilders
import java.util.concurrent.TimeoutException

class GeocodeControllerTest {

    private lateinit var mockMvc: MockMvc
    private lateinit var geocodeRequestProducer: GeocodeRequestProducer
    private lateinit var pendingRequestStore: PendingRequestStore

    @BeforeEach
    fun setUp() {
        geocodeRequestProducer = mock()
        pendingRequestStore = mock()
        val objectMapper = jacksonObjectMapper()
        val controller = GeocodeController(geocodeRequestProducer, pendingRequestStore, objectMapper)
        mockMvc = MockMvcBuilders.standaloneSetup(controller).build()
    }

    @Test
    fun `GET geocode should return 200 with results when agent responds`() {
        whenever(pendingRequestStore.await(any(), eq(30))).thenReturn(
            """{
              "correlation_id":"c1",
              "results":[
                {"latitude":25.0478,"longitude":121.5170,"display_name":"台北車站"}
              ]
            }""".trimIndent()
        )

        mockMvc.get("/api/v1/geocode") {
            param("q", "台北車站")
            param("cityHint", "台北")
        }.andExpect {
            status { isOk() }
            header { string("Content-Type", org.hamcrest.Matchers.containsString("charset=UTF-8")) }
            jsonPath("$.results[0].latitude") { value(25.0478) }
            jsonPath("$.results[0].displayName") { value("台北車站") }
        }

        verify(pendingRequestStore).register(any())
        verify(geocodeRequestProducer).send(any(), eq("台北車站"), eq("台北"), eq(5))
    }

    @Test
    fun `GET geocode should return 400 when q is missing`() {
        mockMvc.get("/api/v1/geocode").andExpect {
            status { isBadRequest() }
            header { string("Content-Type", org.hamcrest.Matchers.containsString("charset=UTF-8")) }
            jsonPath("$.error") { value("q is required") }
        }

        verify(geocodeRequestProducer, never()).send(any(), any(), any(), any())
    }

    @Test
    fun `GET geocode should return 400 when q is blank`() {
        mockMvc.get("/api/v1/geocode") {
            param("q", "   ")
        }.andExpect {
            status { isBadRequest() }
            header { string("Content-Type", org.hamcrest.Matchers.containsString("charset=UTF-8")) }
            jsonPath("$.error") { value("q is required") }
        }

        verify(geocodeRequestProducer, never()).send(any(), any(), any(), any())
    }

    @Test
    fun `GET geocode should clamp limit above 10 down to 10`() {
        whenever(pendingRequestStore.await(any(), eq(30))).thenReturn(
            """{"correlation_id":"c1","results":[]}"""
        )

        mockMvc.get("/api/v1/geocode") {
            param("q", "台北")
            param("limit", "50")
        }.andExpect {
            status { isOk() }
        }

        verify(geocodeRequestProducer).send(any(), eq("台北"), anyOrNull(), eq(10))
    }

    @Test
    fun `GET geocode should clamp limit below 1 up to 1`() {
        whenever(pendingRequestStore.await(any(), eq(30))).thenReturn(
            """{"correlation_id":"c1","results":[]}"""
        )

        mockMvc.get("/api/v1/geocode") {
            param("q", "台北")
            param("limit", "0")
        }.andExpect {
            status { isOk() }
        }

        verify(geocodeRequestProducer).send(any(), eq("台北"), anyOrNull(), eq(1))
    }

    @Test
    fun `GET geocode should return 504 when agent times out`() {
        whenever(pendingRequestStore.await(any(), any())).thenAnswer { throw TimeoutException("timeout") }

        mockMvc.get("/api/v1/geocode") {
            param("q", "台北")
        }.andExpect {
            status { isGatewayTimeout() }
            header { string("Content-Type", org.hamcrest.Matchers.containsString("charset=UTF-8")) }
            jsonPath("$.error") { exists() }
        }
    }
}
