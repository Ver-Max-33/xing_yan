import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// `npm run dev` talks to a backend started with `wb serve --port 8765 --token $WB_TOKEN`;
// the dev proxy adds the token header (Electron sets a cookie instead).
const backend = process.env.WB_BACKEND ?? "http://127.0.0.1:8765"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "./src") } },
  base: "./",
  server: {
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: backend, headers: { "X-WB-Token": process.env.WB_TOKEN ?? "" } } },
  },
  build: { outDir: "dist", chunkSizeWarningLimit: 1500 },
})
