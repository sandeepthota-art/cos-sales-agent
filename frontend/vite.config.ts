import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // Dev-only: FastAPI runs separately (uvicorn app.api.main:app --port 8000).
      // Production serves both from one origin (Phase 14, not yet wired up).
      '/api': 'http://localhost:8000',
    },
  },
})
