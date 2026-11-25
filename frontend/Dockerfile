# ── Build stage ───────────────────────────────────────────────────────────────
FROM node:20-alpine AS builder
WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY . .
RUN npm run build

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM nginx:alpine
WORKDIR /app

# Copy built SPA
COPY --from=builder /app/dist /usr/share/nginx/html

# FIX 4 — COPY the config file instead of inline `echo '...'`.
# The original Dockerfile had Windows line endings (CRLF) throughout, which
# caused nginx to receive literal \r characters inside directive values and
# fail to start with "unknown directive" errors on Linux.
# Keeping the config in a proper file also makes it version-controlled and
# easier to diff/review.
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 3000
CMD ["nginx", "-g", "daemon off;"]
