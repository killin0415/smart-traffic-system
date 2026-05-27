import { useEffect, useState } from 'react'
import { useAppStore } from '../store'
import { postRoute } from '../api/route'
import AddressInput from './AddressInput'

export default function RouteForm() {
  const originMarker = useAppStore((s) => s.originMarker)
  const destMarker = useAppStore((s) => s.destMarker)
  const setOriginMarker = useAppStore((s) => s.setOriginMarker)
  const setDestMarker = useAppStore((s) => s.setDestMarker)
  const setCurrentRoute = useAppStore((s) => s.setCurrentRoute)
  const loading = useAppStore((s) => s.loading)
  const setLoading = useAppStore((s) => s.setLoading)
  const showError = useAppStore((s) => s.showError)

  const [originText, setOriginText] = useState('')
  const [destText, setDestText] = useState('')

  useEffect(() => {
    if (!originMarker) return
    setOriginText((prev) =>
      prev.trim().length > 0
        ? prev
        : `${originMarker[0].toFixed(5)}, ${originMarker[1].toFixed(5)}`,
    )
  }, [originMarker])

  useEffect(() => {
    if (!destMarker) return
    setDestText((prev) =>
      prev.trim().length > 0
        ? prev
        : `${destMarker[0].toFixed(5)}, ${destMarker[1].toFixed(5)}`,
    )
  }, [destMarker])

  const canSubmit = originMarker !== null && destMarker !== null && !loading

  async function handleSubmit() {
    if (!originMarker || !destMarker) return
    setLoading(true)
    try {
      const res = await postRoute({
        originLat: originMarker[0],
        originLng: originMarker[1],
        destLat: destMarker[0],
        destLng: destMarker[1],
        topK: 3,
      })
      if (!res.routes || res.routes.length === 0) {
        showError(res.error ?? '找不到可行路線')
        return
      }
      setCurrentRoute(res)
    } catch {
      // client.ts already showed an error toast
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="space-y-3">
      <AddressInput
        label="起點"
        placeholder="搜尋地址或在地圖上點選"
        focusKey="origin"
        value={originMarker}
        text={originText}
        onTextChange={setOriginText}
        onSelect={({ lat, lng }) => setOriginMarker([lat, lng])}
      />
      <AddressInput
        label="終點"
        placeholder="搜尋地址或在地圖上點選"
        focusKey="dest"
        value={destMarker}
        text={destText}
        onTextChange={setDestText}
        onSelect={({ lat, lng }) => setDestMarker([lat, lng])}
      />
      <button
        type="button"
        disabled={!canSubmit}
        onClick={handleSubmit}
        className={[
          'w-full rounded-md px-4 py-2 text-sm font-medium text-white',
          'bg-blue-600 hover:bg-blue-700',
          'disabled:cursor-not-allowed disabled:bg-slate-400',
        ].join(' ')}
      >
        {loading ? '規劃中…' : '規劃路線'}
      </button>
    </section>
  )
}
