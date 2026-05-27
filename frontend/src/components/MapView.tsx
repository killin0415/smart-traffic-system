import { useEffect } from 'react'
import {
  MapContainer,
  Marker,
  Polyline,
  Popup,
  TileLayer,
  useMap,
  useMapEvents,
} from 'react-leaflet'
import L from 'leaflet'
import { useAppStore } from '../store'
import type { RouteItem } from '../types/api'

const DEFAULT_CENTER: [number, number] = [25.0478, 121.517]
const DEFAULT_ZOOM = 14

function makeColoredIcon(color: string) {
  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" width="28" height="40" viewBox="0 0 28 40">
      <path d="M14 0c7.732 0 14 6.268 14 14 0 9-14 26-14 26S0 23 0 14C0 6.268 6.268 0 14 0z" fill="${color}" stroke="#1f2937" stroke-width="1.5"/>
      <circle cx="14" cy="14" r="5" fill="#ffffff"/>
    </svg>`.trim()
  return L.divIcon({
    html: svg,
    className: 'stm-marker',
    iconSize: [28, 40],
    iconAnchor: [14, 38],
    popupAnchor: [0, -32],
  })
}

const ORIGIN_ICON = makeColoredIcon('#2563eb')
const DEST_ICON = makeColoredIcon('#7c3aed')
const CAMERA_ICON = makeColoredIcon('#dc2626')
const PARKING_ICON = makeColoredIcon('#16a34a')

function getSelectedRoute(
  currentRoute: ReturnType<typeof useAppStore.getState>['currentRoute'],
  index: number,
): RouteItem | null {
  if (!currentRoute || currentRoute.routes.length === 0) return null
  const safeIndex = Math.min(Math.max(0, index), currentRoute.routes.length - 1)
  return currentRoute.routes[safeIndex] ?? null
}

function RouteFitter() {
  const map = useMap()
  const currentRoute = useAppStore((s) => s.currentRoute)
  const selectedRouteIndex = useAppStore((s) => s.selectedRouteIndex)
  const route = getSelectedRoute(currentRoute, selectedRouteIndex)

  useEffect(() => {
    if (!route || route.coordinates.length === 0) return
    const bounds = L.latLngBounds(
      route.coordinates.map(([lat, lng]) => L.latLng(lat, lng)),
    )
    if (bounds.isValid()) {
      map.fitBounds(bounds, { padding: [40, 40] })
    }
  }, [map, route])

  return null
}

function MapClickHandler() {
  const focusedInput = useAppStore((s) => s.focusedInput)
  const setOriginMarker = useAppStore((s) => s.setOriginMarker)
  const setDestMarker = useAppStore((s) => s.setDestMarker)

  useMapEvents({
    click(event) {
      if (!focusedInput) return
      const point: [number, number] = [event.latlng.lat, event.latlng.lng]
      if (focusedInput === 'origin') {
        setOriginMarker(point)
      } else {
        setDestMarker(point)
      }
    },
  })
  return null
}

export default function MapView() {
  const currentRoute = useAppStore((s) => s.currentRoute)
  const selectedRouteIndex = useAppStore((s) => s.selectedRouteIndex)
  const originMarker = useAppStore((s) => s.originMarker)
  const destMarker = useAppStore((s) => s.destMarker)

  const route = getSelectedRoute(currentRoute, selectedRouteIndex)
  const polylinePositions =
    route?.coordinates.map(([lat, lng]) => [lat, lng] as [number, number]) ??
    []

  return (
    <MapContainer
      center={DEFAULT_CENTER}
      zoom={DEFAULT_ZOOM}
      scrollWheelZoom
      className="h-full w-full"
    >
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
      />
      <MapClickHandler />
      <RouteFitter />

      {polylinePositions.length > 1 && (
        <Polyline
          positions={polylinePositions}
          pathOptions={{ color: '#2563eb', weight: 5, opacity: 0.85 }}
        />
      )}

      {originMarker && (
        <Marker position={originMarker} icon={ORIGIN_ICON}>
          <Popup>起點</Popup>
        </Marker>
      )}
      {destMarker && (
        <Marker position={destMarker} icon={DEST_ICON}>
          <Popup>終點</Popup>
        </Marker>
      )}

      {route?.speedCameras.map((cam, i) => (
        <Marker
          key={`cam-${i}`}
          position={[cam.latitude, cam.longitude]}
          icon={CAMERA_ICON}
        >
          <Popup>
            <div className="text-sm">
              <div className="font-semibold">測速照相</div>
              {cam.address && <div>{cam.address}</div>}
              {cam.speedLimit > 0 && <div>速限 {cam.speedLimit} km/h</div>}
            </div>
          </Popup>
        </Marker>
      ))}

      {route?.parkingSuggestions.map((p, i) => (
        <Marker
          key={`park-${p.id ?? i}`}
          position={[p.latitude, p.longitude]}
          icon={PARKING_ICON}
        >
          <Popup>
            <div className="text-sm">
              <div className="font-semibold">{p.name ?? '停車場'}</div>
              {p.address && <div>{p.address}</div>}
              <div>剩餘車位 {p.availableCar}</div>
            </div>
          </Popup>
        </Marker>
      ))}
    </MapContainer>
  )
}
