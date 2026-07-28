# Single-image, full-stack build: one container serves both the dashboard
# (built React app) and the API (FastAPI) on one port. Visitors just hit
# http://<server>/ — there is no separate frontend service to run or point at.

# ---- stage 1: build the React/Vite frontend into static files ----
FROM node:20-alpine AS frontend-build
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- stage 2: Python backend, serving the built frontend as static files ----
FROM python:3.12-slim
WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Chromium + OS-level deps for the JS-rendered scrapers (DevelopmentAid etc.)
RUN playwright install --with-deps chromium

COPY backend/app ./app
COPY --from=frontend-build /fe/dist ./static
RUN mkdir -p data logs
# Data snapshot for platforms with no persistent disk (e.g. the free Render
# mirror): ships a real copy of the data so the deploy isn't empty. On hosts
# that bind-mount ./backend/data (docker-compose on your own server/VM), that
# mount overrides this at runtime — this line is a no-op there.
COPY backend/data_snapshot/opportunities.db ./data/opportunities.db

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
