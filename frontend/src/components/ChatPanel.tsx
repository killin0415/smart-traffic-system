import { useEffect, useRef, useState } from 'react'
import { useAppStore } from '../store'
import { postChatMessage } from '../api/chat'
import ChatMessage from './ChatMessage'

function newId() {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID()
  }
  return Math.random().toString(36).slice(2)
}

export default function ChatPanel() {
  const messages = useAppStore((s) => s.messages)
  const sessionId = useAppStore((s) => s.sessionId)
  const appendMessage = useAppStore((s) => s.appendMessage)
  const setCurrentRoute = useAppStore((s) => s.setCurrentRoute)

  const [draft, setDraft] = useState('')
  const [thinking, setThinking] = useState(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [messages.length, thinking])

  async function handleSend() {
    const content = draft.trim()
    if (content.length === 0 || thinking) return
    setDraft('')
    appendMessage({
      id: newId(),
      role: 'user',
      content,
      ts: Date.now(),
    })
    setThinking(true)
    try {
      const res = await postChatMessage(content, sessionId)
      appendMessage({
        id: newId(),
        role: 'agent',
        content: res.reply,
        ts: Date.now(),
      })
      if (res.routeResult && res.routeResult.routes.length > 0) {
        setCurrentRoute(res.routeResult)
      }
    } catch {
      // toast already shown by client.ts
    } finally {
      setThinking(false)
    }
  }

  return (
    <section className="flex h-full min-h-[260px] flex-col rounded-lg border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-800">
      <header className="border-b border-slate-200 px-3 py-2 text-sm font-semibold dark:border-slate-700">
        Chat 助理
      </header>
      <div
        ref={scrollRef}
        className="flex-1 space-y-2 overflow-y-auto px-3 py-2"
      >
        {messages.length === 0 && (
          <div className="text-xs text-slate-400">
            試試問：「我要從台北車站到忠孝復興」
          </div>
        )}
        {messages.map((m) => (
          <ChatMessage key={m.id} message={m} />
        ))}
        {thinking && (
          <div className="text-xs italic text-slate-500 dark:text-slate-400">
            agent 思考中…
          </div>
        )}
      </div>
      <div className="border-t border-slate-200 p-2 dark:border-slate-700">
        <div className="flex gap-2">
          <input
            type="text"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                handleSend()
              }
            }}
            placeholder="輸入訊息…"
            disabled={thinking}
            className="flex-1 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-600 dark:bg-slate-900"
          />
          <button
            type="button"
            onClick={handleSend}
            disabled={thinking || draft.trim().length === 0}
            className="rounded-md bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-slate-400"
          >
            送出
          </button>
        </div>
      </div>
    </section>
  )
}
