import { useEffect } from 'react'
import { useAppStore } from '../store'

const AUTO_DISMISS_MS = 3000

export default function Toast() {
  const errorToast = useAppStore((s) => s.errorToast)
  const clearError = useAppStore((s) => s.clearError)

  useEffect(() => {
    if (!errorToast) return
    const id = setTimeout(() => clearError(), AUTO_DISMISS_MS)
    return () => clearTimeout(id)
  }, [errorToast, clearError])

  if (!errorToast) return null

  return (
    <div
      role="alert"
      className="pointer-events-auto fixed bottom-4 left-1/2 z-[1000] -translate-x-1/2 rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white shadow-lg"
    >
      {errorToast}
    </div>
  )
}
