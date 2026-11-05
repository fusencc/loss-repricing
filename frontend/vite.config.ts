import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const proxy = { '/api': 'http://127.0.0.1:18765' }

export default defineConfig({
  plugins: [vue()],
  server: { proxy },
  preview: { proxy },
  build: {
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [{ name: 'element-plus', test: /[\\/]element-plus[\\/]/ }],
        },
      },
    },
  },
})
