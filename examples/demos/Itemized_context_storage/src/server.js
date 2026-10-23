import { readFile } from "node:fs/promises";
import path from "node:path";
import { ItemizedDemoStore } from "./storage.js";

function readOptionValue(name) {
  const index = process.argv.indexOf(name);
  if (index === -1) {
    return null;
  }
  const value = process.argv[index + 1];
  if (!value || value.startsWith("--")) {
    throw new Error(`缺少命令行参数 ${name} 的值`);
  }
  return value;
}

const rawPort = process.env.ITEMIZED_CONTEXT_PORT ?? readOptionValue("--port") ?? "8142";
const port = Number(rawPort);
if (!Number.isInteger(port) || port <= 0 || port > 65535) {
  throw new Error(`非法端口: ${rawPort}`);
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
  const relativeToWebRoot = path.relative(webRoot, filePath);
  if (
    relativeToWebRoot === ""
    || relativeToWebRoot === ".."
    || relativeToWebRoot.startsWith(`..${path.sep}`)
    || path.isAbsolute(relativeToWebRoot)
  ) {
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
      return responseJson(await store.readRuntimeSnapshot());
    }
    return serveStatic(url);
  },
});

console.log(`Itemized Context Storage Demo: http://127.0.0.1:${server.port}`);
console.log(`运行时数据: ${store.runtimeRoot}`);

function shutdown(signal) {
  console.log(`[itemized-context] 收到 ${signal}，正在关闭服务`);
  store.close();
  server.stop();
  process.exit(0);
}

process.once("SIGINT", () => shutdown("SIGINT"));
process.once("SIGTERM", () => shutdown("SIGTERM"));
