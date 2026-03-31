import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'
import { outputRunsPlugin } from './vite-plugin-output-runs'

const reportGeneraterRoot = path.resolve(__dirname, '..')
const outputDir = path.join(reportGeneraterRoot, 'output')

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    outputRunsPlugin(outputDir),
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  server: {
    fs: {
      allow: [path.resolve(__dirname, '..')],
    },
  },
})
