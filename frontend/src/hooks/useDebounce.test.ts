import { describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useDebounce } from './useDebounce'

describe('useDebounce', () => {
  it('returns the initial value immediately', () => {
    const { result } = renderHook(() => useDebounce('a', 300))
    expect(result.current).toBe('a')
  })

  it('updates only after the delay elapses', () => {
    vi.useFakeTimers()
    try {
      const { result, rerender } = renderHook(
        ({ v }) => useDebounce(v, 300),
        { initialProps: { v: 'a' } },
      )
      rerender({ v: 'b' })
      expect(result.current).toBe('a')
      act(() => {
        vi.advanceTimersByTime(299)
      })
      expect(result.current).toBe('a')
      act(() => {
        vi.advanceTimersByTime(1)
      })
      expect(result.current).toBe('b')
    } finally {
      vi.useRealTimers()
    }
  })

  it('cancels pending updates when value changes again before the delay', () => {
    vi.useFakeTimers()
    try {
      const { result, rerender } = renderHook(
        ({ v }) => useDebounce(v, 300),
        { initialProps: { v: 'a' } },
      )
      rerender({ v: 'b' })
      act(() => {
        vi.advanceTimersByTime(150)
      })
      rerender({ v: 'c' })
      act(() => {
        vi.advanceTimersByTime(150)
      })
      expect(result.current).toBe('a')
      act(() => {
        vi.advanceTimersByTime(150)
      })
      expect(result.current).toBe('c')
    } finally {
      vi.useRealTimers()
    }
  })
})
