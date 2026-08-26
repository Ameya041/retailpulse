import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Proxy /api to the gateway in development so the browser sees one origin
    // and CORS never enters the picture locally. In production the gateway
    // serves the same role, which keeps dev and prod shaped the same.
    proxy: {
      '/api': {
        target: process.env.VITE_GATEWAY_URL || 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    rollupOptions: {
      output: {
        // Split the two large, rarely-changing dependencies into their own
        // chunks. They then stay cached across deploys instead of being
        // re-downloaded every time application code changes.
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          charts: ['recharts'],
        },
      },
    },
  },
})
