"""
Async gRPC server for agent-service.
Binds to port 50051 and serves the ChatService.
"""
import grpc
from grpc import aio

from src.proto import chat_service_pb2_grpc
from src.grpc_server.chat_servicer import ChatServicer

GRPC_PORT = 50051


async def start_grpc_server():
    """Start the async gRPC server on port 50051."""
    server = aio.server()
    chat_service_pb2_grpc.add_ChatServiceServicer_to_server(ChatServicer(), server)
    listen_addr = f"[::]:{GRPC_PORT}"
    server.add_insecure_port(listen_addr)
    await server.start()
    print(f"[gRPC] Server started on port {GRPC_PORT}")
    return server
