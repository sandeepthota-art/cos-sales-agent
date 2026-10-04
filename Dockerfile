# Stage 1: build the React frontend (Phase 14 static serving -- app/api/main.py
# serves frontend/dist when present; frontend/dist is gitignored and never
# committed, so without this stage no Render service using this image can ever
# serve the React app, regardless of which start command it uses).
FROM node:20-bookworm-slim AS frontend-build

WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ .
RUN npm run build

# Pinned to the Debian "bookworm" codename (not the floating "slim" alias) for a
# current, reproducible OpenSSL/TLS stack -- fixes TLSV1_ALERT_INTERNAL_ERROR
# against MongoDB Atlas caused by an outdated OpenSSL in a stale cached base image.
FROM python:3.11-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend-build /frontend/dist ./frontend/dist

# Default: the MCP server (the long-running service this image is meant to deploy,
# e.g. on Render -- see README.md's "Render deployment" section). It uses stdio
# unless MCP_TRANSPORT=streamable-http is set in the environment, in which case it
# binds 0.0.0.0:$PORT and requires MCP_AUTH_TOKEN.
#
# To run a one-shot pipeline command instead, override the container command, e.g.:
#   docker run --env-file .env cos-sales-agent python main.py --mode=demo
CMD ["python", "-m", "app.mcp.server"]
