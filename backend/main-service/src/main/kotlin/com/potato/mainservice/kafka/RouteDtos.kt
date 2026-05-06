package com.potato.mainservice.kafka

import com.fasterxml.jackson.annotation.JsonIgnoreProperties

/**
 * Kafka `route.response` payload.
 *
 * Mirrors the Python multiagent-service `RouteResponse` schema. Marked
 * `@JsonIgnoreProperties(ignoreUnknown = true)` so future fields added on the
 * Python side won't break deserialization here.
 */
@JsonIgnoreProperties(ignoreUnknown = true)
data class RouteResponse(
    val correlationId: String? = null,
    val routes: List<RouteItem> = emptyList(),
    val error: String? = null,
)

@JsonIgnoreProperties(ignoreUnknown = true)
data class RouteItem(
    val path: List<Int> = emptyList(),
    val edges: List<Int> = emptyList(),
    val roadNames: List<String> = emptyList(),
    val estimatedTimeMin: Double = 0.0,
    val distanceKm: Double = 0.0,
    val speedCameras: List<SpeedCamera> = emptyList(),
    val parkingSuggestions: List<ParkingSuggestion> = emptyList(),
)

@JsonIgnoreProperties(ignoreUnknown = true)
data class SpeedCamera(
    val latitude: Double = 0.0,
    val longitude: Double = 0.0,
    val direction: String? = null,
    val speedLimit: Int = 0,
    val address: String? = null,
)

@JsonIgnoreProperties(ignoreUnknown = true)
data class ParkingSuggestion(
    val id: Int = 0,
    val name: String? = null,
    val address: String? = null,
    val availableCar: Int = 0,
    val distanceM: Double = 0.0,
)
