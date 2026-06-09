package com.potato.mainservice.controller

import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.test.web.servlet.MockMvc
import org.springframework.test.web.servlet.get
import org.springframework.test.web.servlet.setup.MockMvcBuilders

class HealthControllerTest {

    private lateinit var mockMvc: MockMvc

    @BeforeEach
    fun setUp() {
        mockMvc = MockMvcBuilders.standaloneSetup(HealthController()).build()
    }

    @Test
    fun `GET health should return 200`() {
        mockMvc.get("/health").andExpect {
            status { isOk() }
        }
    }

    @Test
    fun `GET health should return SERVING status`() {
        mockMvc.get("/health").andExpect {
            jsonPath("$.main_service") { value("SERVING") }
        }
    }
}
