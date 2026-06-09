# syntax=docker/dockerfile:1.7

# --- 1. Build stage: produce the Vite SPA bundle ---
FROM node:24-alpine AS builder

WORKDIR /app

# Layer-cache npm install on package*.json only.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# --- 2. Runtime stage: nginx serving /usr/share/nginx/html + /api proxy ---
FROM nginx:1.27-alpine AS runtime

# Drop the default nginx config (which serves only the welcome page).
RUN rm /etc/nginx/conf.d/default.conf

COPY nginx.conf /etc/nginx/conf.d/app.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
