# syntax=docker/dockerfile:1.7

# --- 1. Build stage: compile Kotlin + bootJar ---
FROM eclipse-temurin:21-jdk-jammy AS builder

WORKDIR /workspace

# Prime the Gradle wrapper + dependency cache before copying sources, so layer
# caching only invalidates when build.gradle.kts / settings change.
COPY gradlew gradlew.bat settings.gradle.kts build.gradle.kts ./
COPY gradle ./gradle
RUN chmod +x gradlew && ./gradlew --version

# Pull dependencies first (cached layer).
RUN ./gradlew dependencies --no-daemon -q || true

# Now copy sources and build.
COPY src ./src
RUN ./gradlew bootJar --no-daemon -x test \
    && cp build/libs/*.jar /workspace/app.jar

# --- 2. Runtime stage: just JRE + the boot jar ---
FROM eclipse-temurin:21-jre-jammy AS runtime

WORKDIR /app

# Non-root user.
RUN groupadd --system app && useradd --system --gid app --no-create-home app

COPY --from=builder --chown=app:app /workspace/app.jar /app/app.jar

USER app
EXPOSE 8081

# Inside compose, host/port for db, redis, kafka are reachable via service DNS;
# all of these can be overridden by compose env vars. Defaults below match the
# infra/docker-compose.yml service names.
ENV SPRING_DATASOURCE_URL=jdbc:postgresql://timescaledb:5432/traffic_data \
    SPRING_DATASOURCE_USERNAME=admin \
    SPRING_DATASOURCE_PASSWORD=secret \
    SPRING_REDIS_HOST=redis \
    SPRING_KAFKA_BOOTSTRAP_SERVERS=kafka:29092 \
    JAVA_TOOL_OPTIONS="-XX:+UseContainerSupport -XX:MaxRAMPercentage=75"

ENTRYPOINT ["java", "-jar", "/app/app.jar"]
