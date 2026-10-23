import { EventEmitter } from "node:events";
import path from "node:path";
import { PassThrough } from "node:stream";
import { describe, expect, test } from "bun:test";
import { SshTunnelManager, waitForRemoteHealth } from "../src/server/tunnel.js";

function createSshChildFixture({ shouldExit = () => true } = {}) {
  const child = new EventEmitter();
  const signals = [];
  child.stderr = new PassThrough();
  child.kill = (signal) => {
    signals.push(signal);
    if (shouldExit(signal)) {
      queueMicrotask(() => {
        child.emit("exit", null, signal);
        child.stderr.end();
        child.emit("close", null, signal);
      });
    }
    return true;
  };
  return { child, signals };
}

function createTunnelManagerFixture(options = {}) {
  const childFixture = createSshChildFixture(options);
  const loggedErrors = [];
  const target = {
    id: "remote-demo",
    ssh: {
      host: "example.test",
      user: "demo",
      port: 22,
      privateKeyPath: path.resolve("fixture-key"),
      remoteBackendHost: "127.0.0.1",
      remoteBackendPort: 7912,
    },
  };
  const tunnelManager = new SshTunnelManager({
    logger: { error: (message) => loggedErrors.push(message) },
    spawnSsh: () => childFixture.child,
    fetchRemote: async () => new Response(null, { status: 200 }),
    allocatePort: async () => 50_001,
    terminationGraceMs: 5,
    forceTerminationTimeoutMs: 5,
  });
  return { ...childFixture, loggedErrors, target, tunnelManager };
}

describe("SSH 隧道 stderr 日志", () => {
  test("UTF-8 字符跨 Buffer chunk 时保持完整", async () => {
    const { child, loggedErrors, target, tunnelManager } = createTunnelManagerFixture();
    await tunnelManager.backendOriginFor(target);

    const emojiBytes = Buffer.from("🙂");
    child.stderr.write(Buffer.concat([Buffer.from("remote "), emojiBytes.subarray(0, 2)]));
    child.stderr.write(Buffer.concat([emojiBytes.subarray(2), Buffer.from(" connected")]));

    const joinedErrors = loggedErrors.join("\n");
    expect(joinedErrors).toContain("🙂");
    expect(joinedErrors).not.toContain("\uFFFD");
    await tunnelManager.shutdown();
  });
});

describe("SSH 隧道健康检查", () => {
  test("HTTP 重试和健康响应都释放未读取的 body", async () => {
    const cancelledStatuses = [];
    const responses = [503, 200].map((status) => new Response(new ReadableStream({
      cancel() {
        cancelledStatuses.push(status);
      },
    }), { status }));
    let requestCount = 0;

    await waitForRemoteHealth("http://backend.example.test", () => {}, async (url, options) => {
      requestCount += 1;
      expect(url).toBe("http://backend.example.test/health");
      expect(options.signal).toBeDefined();
      return responses.shift();
    });

    expect(requestCount).toBe(2);
    expect(cancelledStatuses).toEqual([503, 200]);
  });
});

describe("SSH 隧道关闭", () => {
  test("等待 SSH 子进程正常退出，并拒绝关闭后的新连接", async () => {
    const { signals, target, tunnelManager } = createTunnelManagerFixture();

    await expect(tunnelManager.backendOriginFor(target)).resolves.toBe("http://127.0.0.1:50001");
    await tunnelManager.shutdown();

    expect(signals).toEqual(["SIGTERM"]);
    expect(() => tunnelManager.backendOriginFor(target)).toThrow(
      "SSH 隧道管理器正在关闭",
    );
  });

  test("宽限期后升级为 SIGKILL，并等待子进程关闭", async () => {
    const { signals, target, tunnelManager } = createTunnelManagerFixture({
      shouldExit: (signal) => signal === "SIGKILL",
    });

    await tunnelManager.backendOriginFor(target);
    await tunnelManager.shutdown();

    expect(signals).toEqual(["SIGTERM", "SIGKILL"]);
  });

  test("无法确认子进程关闭时返回明确错误", async () => {
    const { target, tunnelManager } = createTunnelManagerFixture({ shouldExit: () => false });

    await tunnelManager.backendOriginFor(target);

    let shutdownError = null;
    try {
      await tunnelManager.shutdown();
    } catch (error) {
      shutdownError = error;
    }

    expect(shutdownError).toBeInstanceOf(AggregateError);
    expect(shutdownError.errors.map((error) => error.message)).toEqual([
      expect.stringContaining("已发送 SIGKILL 后等待 5ms"),
    ]);
  });
});
