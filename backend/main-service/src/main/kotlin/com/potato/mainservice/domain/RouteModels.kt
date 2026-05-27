package com.potato.mainservice.domain

/**
 * HTTP request body for `POST /api/v1/route`. Camel-cased to match the
 * frontend TypeScript convention; the producer translates these to snake_case
 * before publishing to `route.request`.
 *
 * Coordinates are nullable so a missing field can be distinguished from `0.0`
 * (and surfaced as HTTP 400 in the controller).
 */
data class RouteRequest(
    val originLat: Double? = null,
    val originLng: Double? = null,
    val destLat: Double? = null,
    val destLng: Double? = null,
    val topK: Int? = 3,
)
