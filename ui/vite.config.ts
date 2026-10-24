import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In dev, /api is proxied to the API on the host (compose publishes it on 8001).
// In the container, nginx does the same proxying (see nginx.conf).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: process.env.API_URL ?? 'http://localhost:8001',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
