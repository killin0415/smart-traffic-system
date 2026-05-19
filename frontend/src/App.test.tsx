import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

// react-leaflet pulls in Leaflet which touches DOM/canvas APIs that jsdom
// doesn't implement. Stub the components we use so the smoke test stays fast
// and focused on App layout.
vi.mock('react-leaflet', () => {
  const Passthrough = ({ children }: { children?: React.ReactNode }) => (
    <div data-testid="leaflet-stub">{children}</div>
  )
  return {
    MapContainer: Passthrough,
    TileLayer: () => null,
    Marker: Passthrough,
    Popup: Passthrough,
    Polyline: () => null,
    useMap: () => ({ fitBounds: vi.fn() }),
    useMapEvents: () => ({}),
  }
})

import App from './App'

describe('<App />', () => {
  it('renders the route form, chat panel, and a map placeholder', () => {
    render(<App />)
    expect(screen.getByText('智慧交通導航 · Demo')).toBeInTheDocument()
    expect(screen.getByText('起點')).toBeInTheDocument()
    expect(screen.getByText('終點')).toBeInTheDocument()
    expect(screen.getByText('規劃路線')).toBeInTheDocument()
    expect(screen.getByText('Chat 助理')).toBeInTheDocument()
    expect(screen.getAllByTestId('leaflet-stub').length).toBeGreaterThan(0)
  })
})
