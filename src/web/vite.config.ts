import react from '@vitejs/plugin-react-swc';
import path from 'path';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [
    react(),
    {
      name: 'frontend-health-route',
      configureServer(server) {
        server.middlewares.use((req, res, next) => {
          if (req.method === 'GET' && req.url === '/health') {
            res.statusCode = 200;
            res.setHeader('Content-Type', 'application/json; charset=utf-8');
            res.end(JSON.stringify({ status: 'ok', service: 'frontend' }));
            return;
          }

          next();
        });
      },
    },
  ],
  server: {
    host: '127.0.0.1',
    port: 8001,
    strictPort: true,
    hmr: true,
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: path.resolve(process.cwd(), 'index.html'),
        preview: path.resolve(process.cwd(), 'preview.html'),
      },
      output: {
        entryFileNames: 'assets/[name].js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/[name].[ext]',
      },
    },
  },
  base: './',
});
