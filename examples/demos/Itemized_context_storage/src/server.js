import { readFile } from "node:fs/promises";
import path from "node:path";
import { ItemizedDemoStore } from "./storage.js";

const port = Number(process.env.ITEMIZED_CONTEXT_PORT ?? "8142");
if (!Number.isInteger(port) || port <= 0 || port > 65535) {
  throw new Error(`非法端口: ${port}`);
}

const webRoot = path.resolve(import.meta.dir, "web");
const store = new ItemizedDemoStore("demo");
await store.initialize();

function responseJson(value) {
  return new Response(JSON.stringify(value), {
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

async function serveStatic(url) {
  const relativePath = url.pathname === "/" ? "index.html" : decodeURIComponent(url.pathname.slice(1));
  const filePath = path.resolve(webRoot, relativePath);
  if (!filePath.startsWith(`${webRoot}${path.sep}`)) {
    throw new Error(`静态资源路径越界: ${url.pathname}`);
  }
  const file = Bun.file(filePath);
  if (!(await file.exists())) {
    return new Response("Not Found", { status: 404 });
  }
  return new Response(file, {
    headers: { "content-type": file.type || "application/octet-stream" },
  });
}

const server = Bun.serve({
  port,
  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/api/state") {
      return responseJson(await store.inspect());
    }
    return serveStatic(url);
  },
});

console.log(`Itemized Context Storage Demo: http://127.0.0.1:${server.port}`);
console.log(`运行时数据: ${store.runtimeRoot}`);

process.on("SIGINT", () => {
  store.close();
  server.stop();
  process.exit(0);
});
