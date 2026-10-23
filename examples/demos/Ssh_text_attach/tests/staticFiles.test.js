import { describe, expect, test } from "bun:test";
import { fileURLToPath } from "node:url";
import { createStaticFileHandler } from "../src/server/staticFiles.js";

const clientRoot = fileURLToPath(new URL("../src/client/", import.meta.url));

function createResponse() {
  return {
    statusCode: null,
    headers: null,
    body: null,
    writeHead(statusCode, headers) {
      this.statusCode = statusCode;
      this.headers = headers;
    },
    end(body) {
      this.body = body;
    },
  };
}

describe("静态前端服务", () => {
  test("从实际客户端目录返回静态资源", async () => {
    const handler = createStaticFileHandler(clientRoot, 8123);
    const response = createResponse();

    await handler({ url: "/index.html" }, response);

    expect(response.statusCode).toBe(200);
    expect(response.headers["content-type"]).toBe("text/html; charset=utf-8");
    expect(response.body).toBeInstanceOf(Buffer);
    expect(response.body.byteLength).toBeGreaterThan(0);
  });

  test("拒绝百分号编码路径分隔符形成的静态目录穿越", async () => {
    const handler = createStaticFileHandler(clientRoot, 8123);
    const response = createResponse();

    await handler({ url: "/%2e%2e%2fsrc%2fserver%2fserver.js" }, response);

    expect(response.statusCode).toBe(500);
    expect(response.body).toContain("静态资源路径越界");
  });

  test("运行配置返回当前 Gateway 端口", async () => {
    const handler = createStaticFileHandler(process.cwd(), 8123);
    const response = createResponse();

    await handler({ url: "/runtime-config" }, response);

    expect(response.statusCode).toBe(200);
    expect(response.headers["cache-control"]).toBe("no-store");
    expect(JSON.parse(response.body)).toEqual({ gatewayPort: 8123 });
  });
});
