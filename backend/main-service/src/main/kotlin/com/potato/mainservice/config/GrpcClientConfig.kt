package com.potato.mainservice.config

import io.grpc.ManagedChannel
import io.grpc.ManagedChannelBuilder
import com.potato.mainservice.grpc.ChatServiceGrpc
import org.springframework.beans.factory.annotation.Value
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration
import jakarta.annotation.PreDestroy

/**
 * Configuration for gRPC channel to agent-service.
 */
@Configuration
class GrpcClientConfig(
    @Value("\${grpc.agent-service.host}") private val host: String,
    @Value("\${grpc.agent-service.port}") private val port: Int,
) {
    private lateinit var channel: ManagedChannel

    @Bean
    fun grpcChannel(): ManagedChannel {
        channel = ManagedChannelBuilder
            .forAddress(host, port)
            .usePlaintext()
            .build()
        return channel
    }

    @Bean
    fun chatServiceStub(channel: ManagedChannel): ChatServiceGrpc.ChatServiceBlockingStub {
        return ChatServiceGrpc.newBlockingStub(channel)
    }

    @PreDestroy
    fun shutdownChannel() {
        if (::channel.isInitialized) {
            channel.shutdown()
        }
    }
}
