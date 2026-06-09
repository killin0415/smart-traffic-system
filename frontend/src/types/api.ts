export interface SpeedCamera {
  latitude: number
  longitude: number
  direction?: string | null
  speedLimit: number
  address?: string | null
}

export interface ParkingSuggestion {
  id: number
  name?: string | null
  address?: string | null
  latitude: number
  longitude: number
  availableCar: number
  distanceM: number
}

export interface RouteItem {
  path: number[]
  edges: number[]
  coordinates: [number, number][]
  roadNames: string[]
  estimatedTimeMin: number
  distanceKm: number
  speedCameras: SpeedCamera[]
  parkingSuggestions: ParkingSuggestion[]
}

export interface RouteResponse {
  correlationId?: string | null
  routes: RouteItem[]
  error?: string | null
}

export interface RouteRequest {
  originLat: number
  originLng: number
  destLat: number
  destLng: number
  topK?: number
}

export interface GeocodeResult {
  latitude: number
  longitude: number
  displayName: string
}

export interface GeocodeResponse {
  results: GeocodeResult[]
  error?: string | null
}

export interface ChatMessageRequest {
  session_id: string
  content: string
}

export interface ChatMessageResponse {
  reply: string
  suggested_actions: string[]
  routeResult?: RouteResponse | null
}
