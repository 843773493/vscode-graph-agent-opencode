import react from "@vitejs/plugin-react-swc";
import path from "path";
import { defineConfig } from "vite";

function resolvePort(name: string, fallback: number): number {
  const rawValue = process.env[name]?.trim();
  if (!rawValue) return fallback;
  const value = Number(rawValue);
  if (!Number.isSafeInteger(value) || value < 1 || value > 65535) {
    throw new Error(`${name} 必须是 1 到 65535 的整数，实际为 ${rawValue}`);
  }
  return value;
}

const frontendPort = resolvePort("BOXTEAM_DEV_FRONTEND_PORT", 8011);
const gatewayPort = resolvePort("BOXTEAM_GATEWAY_PORT", 8014);

export default defineConfig({
  cacheDir: process.env.BOXTEAM_VITE_CACHE_DIR ?? "node_modules/.vite",
  plugins: [
    react(),
    {
      name: "frontend-health-route",
      configureServer(server) {
        server.middlewares.use((req, res, next) => {
          if (req.method === "GET" && req.url === "/health") {
            res.statusCode = 200;
            res.setHeader("Content-Type", "application/json; charset=utf-8");
            res.end(JSON.stringify({ status: "ok", service: "frontend" }));
            return;
          }

          next();
        });
      },
    },
  ],
  server: {
    host: "0.0.0.0",
    port: frontendPort,
    strictPort: true,
    hmr: true,
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${gatewayPort}`,
        changeOrigin: true,
        xfwd: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: path.resolve(process.cwd(), "index.html"),
        preview: path.resolve(process.cwd(), "preview.html"),
      },
      output: {
        entryFileNames: "assets/[name].js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name].[ext]",
      },
    },
  },
  base: "./",
});
