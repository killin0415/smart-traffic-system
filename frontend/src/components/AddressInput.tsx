import { useEffect, useRef, useState } from 'react'
import { useDebounce } from '../hooks/useDebounce'
import { geocode } from '../api/geocode'
import { useAppStore } from '../store'
import type { FocusedInput } from '../store'
import type { GeocodeResult } from '../types/api'

interface Props {
  label: string
  placeholder?: string
  focusKey: NonNullable<FocusedInput>
  value: [number, number] | null
  text: string
  onTextChange(text: string): void
  onSelect(result: { lat: number; lng: number; displayName: string }): void
}

export default function AddressInput({
  label,
  placeholder,
  focusKey,
  value,
  text,
  onTextChange,
  onSelect,
}: Props) {
  const setFocusedInput = useAppStore((s) => s.setFocusedInput)
  const focusedInput = useAppStore((s) => s.focusedInput)
  const [suggestions, setSuggestions] = useState<GeocodeResult[]>([])
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const wrapperRef = useRef<HTMLDivElement | null>(null)
  const debouncedText = useDebounce(text, 300)
  const lastQueryRef = useRef<string>('')

  useEffect(() => {
    const q = debouncedText.trim()
    if (q.length < 2) {
      setSuggestions([])
      return
    }
    if (q === lastQueryRef.current) return
    lastQueryRef.current = q
    const ac = new AbortController()
    setLoading(true)
    geocode(q, { limit: 5, signal: ac.signal })
      .then((results) => {
        setSuggestions(results)
        setOpen(results.length > 0)
      })
      .catch(() => {
        setSuggestions([])
      })
      .finally(() => setLoading(false))
    return () => ac.abort()
  }, [debouncedText])

  useEffect(() => {
    function onDocClick(ev: MouseEvent) {
      if (
        wrapperRef.current &&
        !wrapperRef.current.contains(ev.target as Node)
      ) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [])

  const isFocused = focusedInput === focusKey

  return (
    <div ref={wrapperRef} className="relative">
      <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">
        {label}
      </label>
      <input
        type="text"
        value={text}
        placeholder={placeholder}
        onFocus={() => setFocusedInput(focusKey)}
        onChange={(e) => {
          onTextChange(e.target.value)
          setOpen(true)
        }}
        className={[
          'w-full rounded-md border px-3 py-2 text-sm',
          'bg-white dark:bg-slate-800',
          'border-slate-300 dark:border-slate-700',
          'focus:outline-none focus:ring-2',
          isFocused ? 'ring-2 ring-blue-500' : '',
        ].join(' ')}
      />
      {value && (
        <div className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
          {value[0].toFixed(5)}, {value[1].toFixed(5)}
        </div>
      )}
      {open && suggestions.length > 0 && (
        <ul className="absolute z-10 mt-1 max-h-56 w-full overflow-auto rounded-md border border-slate-200 bg-white shadow-lg dark:border-slate-700 dark:bg-slate-800">
          {suggestions.map((s, i) => (
            <li key={`${s.latitude}-${s.longitude}-${i}`}>
              <button
                type="button"
                onClick={() => {
                  onSelect({
                    lat: s.latitude,
                    lng: s.longitude,
                    displayName: s.displayName,
                  })
                  onTextChange(s.displayName)
                  setOpen(false)
                }}
                className="block w-full px-3 py-2 text-left text-sm hover:bg-slate-100 dark:hover:bg-slate-700"
              >
                {s.displayName}
              </button>
            </li>
          ))}
        </ul>
      )}
      {loading && (
        <div className="absolute right-3 top-8 text-xs text-slate-400">
          ⋯
        </div>
      )}
    </div>
  )
}
