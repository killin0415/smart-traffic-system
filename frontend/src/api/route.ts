import { apiCall } from './client'
import type { RouteRequest, RouteResponse } from '../types/api'

export function postRoute(req: RouteRequest): Promise<RouteResponse> {
  return apiCall<RouteResponse>('/api/v1/route', {
    method: 'POST',
    body: req,
  })
}
