import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In Docker, set VITE_BACKEND_URL / VITE_BACKEND_WS_URL to the backend service.
const apiTarget = process.env.VITE_BACKEND_URL || "http://127.0.0.1:8000";
const wsTarget = process.env.VITE_BACKEND_WS_URL || "ws://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.VITE_DEV_PORT) || 5173,
    host: true,
    proxy: {
      "/api": { target: apiTarget, changeOrigin: true },
      "/ws": { target: wsTarget, ws: true },
    },
  },
});
