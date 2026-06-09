import MapView from './components/MapView'
import RouteForm from './components/RouteForm'
import RouteSummary from './components/RouteSummary'
import ChatPanel from './components/ChatPanel'
import ThemeToggle from './components/ThemeToggle'
import Toast from './components/Toast'

export default function App() {
  return (
    <div className="flex h-screen flex-col bg-slate-50 text-slate-900 dark:bg-slate-900 dark:text-slate-100">
      <header className="flex items-center justify-between border-b border-slate-200 px-4 py-2 dark:border-slate-700">
        <h1 className="text-sm font-semibold tracking-tight">
          智慧交通導航 · Demo
        </h1>
        <ThemeToggle />
      </header>
      <main className="flex flex-1 flex-col gap-3 overflow-hidden p-3 md:flex-row">
        <aside className="flex w-full shrink-0 flex-col gap-3 md:w-[360px] md:max-w-[360px]">
          <RouteForm />
          <RouteSummary />
          <div className="min-h-[260px] flex-1">
            <ChatPanel />
          </div>
        </aside>
        <section className="flex-1 overflow-hidden rounded-lg border border-slate-200 dark:border-slate-700">
          <MapView />
        </section>
      </main>
      <Toast />
    </div>
  )
}
