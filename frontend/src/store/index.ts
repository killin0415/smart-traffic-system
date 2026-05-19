import { create } from 'zustand'
import type { RouteResponse } from '../types/api'

export interface ChatMessage {
  id: string
  role: 'user' | 'agent'
  content: string
  ts: number
}

export type FocusedInput = 'origin' | 'dest' | null
export type Theme = 'light' | 'dark'

interface RouteSlice {
  currentRoute: RouteResponse | null
  selectedRouteIndex: number
  originMarker: [number, number] | null
  destMarker: [number, number] | null
  setCurrentRoute(route: RouteResponse | null): void
  setSelectedRouteIndex(index: number): void
  setOriginMarker(marker: [number, number] | null): void
  setDestMarker(marker: [number, number] | null): void
}

interface ChatSlice {
  messages: ChatMessage[]
  sessionId: string
  appendMessage(message: ChatMessage): void
}

interface UiSlice {
  loading: boolean
  errorToast: string | null
  theme: Theme
  focusedInput: FocusedInput
  setLoading(loading: boolean): void
  showError(message: string): void
  clearError(): void
  setTheme(theme: Theme): void
  toggleTheme(): void
  setFocusedInput(focused: FocusedInput): void
}

export type AppState = RouteSlice & ChatSlice & UiSlice

const STORAGE_KEY_SESSION = 'sid'
const STORAGE_KEY_THEME = 'theme'

function initialSessionId(): string {
  if (typeof window === 'undefined') return 'ssr-placeholder'
  const existing = window.localStorage.getItem(STORAGE_KEY_SESSION)
  if (existing) return existing
  const generated =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2)
  window.localStorage.setItem(STORAGE_KEY_SESSION, generated)
  return generated
}

function initialTheme(): Theme {
  if (typeof window === 'undefined') return 'light'
  const stored = window.localStorage.getItem(STORAGE_KEY_THEME)
  if (stored === 'dark' || stored === 'light') return stored
  if (window.matchMedia?.('(prefers-color-scheme: dark)').matches) return 'dark'
  return 'light'
}

function applyThemeToDocument(theme: Theme) {
  if (typeof document === 'undefined') return
  const root = document.documentElement
  if (theme === 'dark') {
    root.classList.add('dark')
  } else {
    root.classList.remove('dark')
  }
}

export const useAppStore = create<AppState>((set, get) => ({
  currentRoute: null,
  selectedRouteIndex: 0,
  originMarker: null,
  destMarker: null,
  setCurrentRoute(route) {
    if (route === null) {
      set({
        currentRoute: null,
        selectedRouteIndex: 0,
        originMarker: null,
        destMarker: null,
      })
      return
    }
    // Anchor origin/dest markers to the actual route endpoints (the snapped
    // start/end nodes), so chat-driven routes show markers and form-driven
    // routes reflect snap-to-graph adjustments.
    const firstRoute = route.routes[0]
    const coords = firstRoute?.coordinates ?? []
    if (coords.length >= 2) {
      const first = coords[0]
      const last = coords[coords.length - 1]
      set({
        currentRoute: route,
        selectedRouteIndex: 0,
        originMarker: [first[0], first[1]],
        destMarker: [last[0], last[1]],
      })
      return
    }
    set({ currentRoute: route, selectedRouteIndex: 0 })
  },
  setSelectedRouteIndex(index) {
    set({ selectedRouteIndex: index })
  },
  setOriginMarker(marker) {
    set({ originMarker: marker })
  },
  setDestMarker(marker) {
    set({ destMarker: marker })
  },

  messages: [],
  sessionId: initialSessionId(),
  appendMessage(message) {
    set((state) => ({ messages: [...state.messages, message] }))
  },

  loading: false,
  errorToast: null,
  theme: initialTheme(),
  focusedInput: 'origin',
  setLoading(loading) {
    set({ loading })
  },
  showError(message) {
    set({ errorToast: message })
  },
  clearError() {
    set({ errorToast: null })
  },
  setTheme(theme) {
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(STORAGE_KEY_THEME, theme)
    }
    applyThemeToDocument(theme)
    set({ theme })
  },
  toggleTheme() {
    const next: Theme = get().theme === 'dark' ? 'light' : 'dark'
    get().setTheme(next)
  },
  setFocusedInput(focused) {
    set({ focusedInput: focused })
  },
}))

export function initializeTheme() {
  applyThemeToDocument(useAppStore.getState().theme)
}
