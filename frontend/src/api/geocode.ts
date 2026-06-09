import { apiCall } from './client'
import type { GeocodeResponse, GeocodeResult } from '../types/api'

interface GeocodeOptions {
  cityHint?: string
  limit?: number
  signal?: AbortSignal
}

export async function geocode(
  q: string,
  opts: GeocodeOptions = {},
): Promise<GeocodeResult[]> {
  const { cityHint, limit, signal } = opts
  const res = await apiCall<GeocodeResponse>('/api/v1/geocode', {
    query: { q, cityHint, limit },
    signal,
    suppressToast: true,
  })
  return res.results ?? []
}
