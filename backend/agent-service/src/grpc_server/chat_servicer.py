"""
gRPC ChatService implementation.
Handles incoming gRPC requests from Spring Boot main-service.
"""
import grpc
from src.proto import chat_service_pb2
from src.proto import chat_service_pb2_grpc


class ChatServicer(chat_service_pb2_grpc.ChatServiceServicer):
    """Implements the ChatService gRPC interface."""

    async def SendMessage(self, request, context):
        """
        Handle a chat message from the main-service.
        Currently returns a stub response; will be wired to MCP/LLM in Phase 4.
        """
        print(f"[gRPC] Received message: session_id={request.session_id}, content={request.content}")

        # TODO: Phase 4 - Route to Chat Manager → MCP Tools → LLM
        reply_text = f"[Agent Service] 收到您的訊息: '{request.content}'. AI 推論功能開發中..."

        return chat_service_pb2.ChatResponse(
            reply=reply_text,
            suggested_actions=["查看即時路況", "規劃路線", "查詢停車位"]
        )

    async def HealthCheck(self, request, context):
        """Simple health check for connectivity verification."""
        return chat_service_pb2.HealthCheckResponse(
            status="SERVING",
            service_name="agent-service"
        )
