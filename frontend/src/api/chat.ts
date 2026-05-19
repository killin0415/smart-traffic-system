import { apiCall } from './client'
import type { ChatMessageRequest, ChatMessageResponse } from '../types/api'

export function postChatMessage(
  content: string,
  sessionId: string,
): Promise<ChatMessageResponse> {
  const body: ChatMessageRequest = { session_id: sessionId, content }
  return apiCall<ChatMessageResponse>('/api/v1/chat/message', {
    method: 'POST',
    body,
  })
}
