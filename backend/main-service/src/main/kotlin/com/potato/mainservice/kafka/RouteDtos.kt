package com.potato.mainservice.kafka

import com.fasterxml.jackson.annotation.JsonAlias
import com.fasterxml.jackson.annotation.JsonIgnoreProperties

/**
 * Kafka `route.response` payload.
 *
 * Mirrors the Python multiagent-service `RouteResponse` schema. Marked
 * `@JsonIgnoreProperties(ignoreUnknown = true)` so future fields added on the
 * Python side won't break deserialization here.
 *
 * The Python side serialises with snake_case (`estimated_time_min`,
 * `road_names`, ...) while these Kotlin DTOs use camelCase for HTTP responses
 * to the frontend. The `@JsonAlias` annotations let Jackson accept both names
 * on the way in; serialisation continues to produce camelCase.
 */
@JsonIgnoreProperties(ignoreUnknown = true)
data class RouteResponse(
    @JsonAlias("correlation_id")
    val correlationId: String? = null,
    val routes: List<RouteItem> = emptyList(),
    val error: String? = null,
)

@JsonIgnoreProperties(ignoreUnknown = true)
data class RouteItem(
    val path: List<Int> = emptyList(),
    val edges: List<Int> = emptyList(),
    val coordinates: List<List<Double>> = emptyList(),
    @JsonAlias("road_names")
    val roadNames: List<String> = emptyList(),
    @JsonAlias("estimated_time_min")
    val estimatedTimeMin: Double = 0.0,
    @JsonAlias("distance_km")
    val distanceKm: Double = 0.0,
    @JsonAlias("speed_cameras")
    val speedCameras: List<SpeedCamera> = emptyList(),
    @JsonAlias("parking_suggestions")
    val parkingSuggestions: List<ParkingSuggestion> = emptyList(),
)

@JsonIgnoreProperties(ignoreUnknown = true)
data class SpeedCamera(
    val latitude: Double = 0.0,
    val longitude: Double = 0.0,
    val direction: String? = null,
    @JsonAlias("speed_limit")
    val speedLimit: Int = 0,
    val address: String? = null,
)

@JsonIgnoreProperties(ignoreUnknown = true)
data class ParkingSuggestion(
    val id: Int = 0,
    val name: String? = null,
    val address: String? = null,
    val latitude: Double = 0.0,
    val longitude: Double = 0.0,
    @JsonAlias("available_car")
    val availableCar: Int = 0,
    @JsonAlias("distance_m")
    val distanceM: Double = 0.0,
)
