import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { test } from "node:test";

for (const failure of ["sync", "async", "logger"]) {
  test(`ACK 后关闭任务失败须记录原错并退出: ${failure}`, () => {
    const child = spawnSync(process.execPath, ["--input-type=module", "-e", `
      import http from "node:http";
      import { writeSync } from "node:fs";
      import { handleRuntimeShutdownRequest } from ${JSON.stringify(new URL("./serverRuntimeControl.mjs", import.meta.url).href)};
      const original = new Error("shutdown-original");
      // 模拟已有全局监听器重入关闭任务后早退，不能让它掩盖本次拒绝。
      process.on("unhandledRejection", () => writeSync(1, "unhandled-rejection\\n"));
      process.on("uncaughtException", () => writeSync(1, "uncaught-exception\\n"));
      const failure = ${JSON.stringify(failure)};
      const server = http.createServer((request, response) => {
        void handleRuntimeShutdownRequest(request, response, {
          pathname: "/internal/runtime/shutdown", workspaceId: "workspace-test",
          readJson: async () => ({ process_id: process.pid, workspace_id: "workspace-test" }),
          shutdown: failure === "sync" ? () => { throw original; } : async () => { throw original; },
          logger: { error(message, error) {
            writeSync(1, JSON.stringify({ message, original: error === original, error: error.message }) + "\\n");
            if (failure === "logger") throw new Error("logger-original");
          } },
        });
      });
      server.listen(0, "127.0.0.1", () => {
        http.request({ host: "127.0.0.1", port: server.address().port, method: "POST",
          headers: { "x-local-token": "shutdown-test-token" } }, response => response.resume()).end();
      });
      setTimeout(() => process.exit(9), 1000);
    `], { env: { ...process.env, BOXTEAM_RUNTIME_CONTROL_TOKEN: "shutdown-test-token" },
      encoding: "utf8", timeout: 5000 });
    assert.ifError(child.error);
    assert.equal(child.status, 1, child.stdout + child.stderr);
    assert.equal(child.signal, null);
    const diagnostic = JSON.parse(child.stdout.trim());
    assert.deepEqual(diagnostic, { message: "本机控制关闭发生未处理错误", original: true, error: "shutdown-original" });
    assert.equal(child.stderr, "");
  });
}
