import { useAppStore } from '../store'

export default function RouteSummary() {
  const currentRoute = useAppStore((s) => s.currentRoute)
  const selectedRouteIndex = useAppStore((s) => s.selectedRouteIndex)
  const setSelectedRouteIndex = useAppStore((s) => s.setSelectedRouteIndex)

  if (!currentRoute || currentRoute.routes.length === 0) {
    return null
  }

  const safeIndex = Math.min(
    Math.max(0, selectedRouteIndex),
    currentRoute.routes.length - 1,
  )
  const route = currentRoute.routes[safeIndex]
  const distanceKm = route.distanceKm.toFixed(1)
  const timeMin = Math.round(route.estimatedTimeMin)

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm dark:border-slate-700 dark:bg-slate-800">
      <h3 className="mb-2 text-sm font-semibold text-slate-700 dark:text-slate-200">
        路線資訊
      </h3>

      {currentRoute.routes.length > 1 && (
        <div className="mb-3 flex gap-1">
          {currentRoute.routes.map((_, i) => (
            <button
              key={i}
              type="button"
              onClick={() => setSelectedRouteIndex(i)}
              className={[
                'flex-1 rounded-md px-2 py-1 text-xs',
                i === safeIndex
                  ? 'bg-blue-600 text-white'
                  : 'bg-slate-100 text-slate-700 hover:bg-slate-200 dark:bg-slate-700 dark:text-slate-200 dark:hover:bg-slate-600',
              ].join(' ')}
            >
              路線 {i + 1}
            </button>
          ))}
        </div>
      )}

      <dl className="grid grid-cols-2 gap-2 text-sm">
        <div>
          <dt className="text-xs text-slate-500 dark:text-slate-400">距離</dt>
          <dd className="font-semibold">{distanceKm} km</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500 dark:text-slate-400">預估時間</dt>
          <dd className="font-semibold">{timeMin} 分鐘</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500 dark:text-slate-400">測速照相</dt>
          <dd className="font-semibold">{route.speedCameras.length} 處</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500 dark:text-slate-400">建議停車場</dt>
          <dd className="font-semibold">{route.parkingSuggestions.length} 處</dd>
        </div>
      </dl>

      {route.roadNames.length > 0 && (
        <div className="mt-3 text-xs text-slate-500 dark:text-slate-400">
          途經：{route.roadNames.slice(0, 5).join('、')}
          {route.roadNames.length > 5 ? ' …' : ''}
        </div>
      )}

      {route.parkingSuggestions.length > 0 && (
        <ul className="mt-3 space-y-1 text-xs">
          {route.parkingSuggestions.map((p, i) => (
            <li
              key={p.id ?? i}
              className="flex items-start justify-between gap-2"
            >
              <span className="text-slate-700 dark:text-slate-200">
                {p.name ?? '停車場'}
              </span>
              <span className="shrink-0 text-slate-500 dark:text-slate-400">
                {p.availableCar} 位 · {Math.round(p.distanceM)} m
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
