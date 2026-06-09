import type { ChatMessage as ChatMessageType } from '../store'

interface Props {
  message: ChatMessageType
}

export default function ChatMessage({ message }: Props) {
  const isUser = message.role === 'user'
  return (
    <div
      className={[
        'flex w-full',
        isUser ? 'justify-end' : 'justify-start',
      ].join(' ')}
    >
      <div
        className={[
          'max-w-[85%] whitespace-pre-wrap rounded-lg px-3 py-2 text-sm',
          isUser
            ? 'bg-blue-600 text-white'
            : 'bg-slate-100 text-slate-900 dark:bg-slate-700 dark:text-slate-100',
        ].join(' ')}
      >
        {message.content}
      </div>
    </div>
  )
}
