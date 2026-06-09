package com.potato.mainservice.domain

import com.fasterxml.jackson.annotation.JsonAlias
import com.fasterxml.jackson.annotation.JsonIgnoreProperties

@JsonIgnoreProperties(ignoreUnknown = true)
data class GeocodeResult(
    val latitude: Double = 0.0,
    val longitude: Double = 0.0,
    @JsonAlias("display_name")
    val displayName: String = "",
)

@JsonIgnoreProperties(ignoreUnknown = true)
data class GeocodeResponse(
    val results: List<GeocodeResult> = emptyList(),
    val error: String? = null,
)
