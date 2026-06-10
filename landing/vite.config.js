import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path, { dirname } from 'path'
import { fileURLToPath } from 'url'
import { appNamePlugin } from '../viteAppNamePlugin.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), appNamePlugin({ envDir: __dirname })],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
})
