import { describe, expect, spyOn, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import path from "node:path";

import {
  buildTransientDevelopmentUnit,
  waitForGateway,
} from "./dev-systemd.mjs";

const projectRoot = process.cwd();
const runId = `${new Date().toISOString().replaceAll(/[-:.TZ]/g, "")}-${process.pid}-${randomUUID()}`;
const fixtureRoot = path.resolve(
  projectRoot,
  "out/tests/scripts/launch/dev-systemd-readiness",
);
const workspaceRunRoot = path.join(fixtureRoot, "workspace", runId);
const artifactsRunRoot = path.join(fixtureRoot, "artifacts", runId);

async function withGatewayFixture(scenario, handler, exercise) {
  const workspace = path.join(workspaceRunRoot, scenario);
  const artifacts = path.join(artifactsRunRoot, scenario);
  const boxteamHome = path.join(workspace, "boxteam-home");
  mkdirSync(workspaceRunRoot, { recursive: true });
  mkdirSync(artifactsRunRoot, { recursive: true });
  mkdirSync(workspace);
  mkdirSync(artifacts);
  mkdirSync(path.join(boxteamHome, "state"), { recursive: true });

  const raw = {
    scenario,
    requests: 0,
    events: [],
    systemctlCalls: [],
  };
  const closedSocketIndexes = new Set();
  const socketCloseWaiters = new Set();
  let managerPid = 4141;
  let requestCount = 0;
  const server = createServer((request, response) => {
    const requestIndex = ++requestCount;
    raw.requests = requestCount;
    raw.events.push(`request:${requestIndex}`);
    request.socket.once("close", () => {
      raw.events.push(`socket-close:${requestIndex}`);
      closedSocketIndexes.add(requestIndex);
      for (const notify of socketCloseWaiters) notify();
    });
    handler(request, response, {
      requestIndex,
      raw,
      setManagerPid(pid) {
        managerPid = pid;
      },
    });
  });

  let spawnSyncSpy;
  let fetchSpy;
  const scheduledTimers = new Set();
  const scheduleTimeout = (callback, delayMs) => {
    const timer = setTimeout(() => {
      scheduledTimers.delete(timer);
      callback();
    }, delayMs);
    scheduledTimers.add(timer);
    return timer;
  };
  const waitForSocketClose = (requestIndex, timeoutMs = 500) => {
    if (closedSocketIndexes.has(requestIndex)) return Promise.resolve(true);
    return new Promise((resolve) => {
      let timer;
      const finish = (closed) => {
        clearTimeout(timer);
        socketCloseWaiters.delete(check);
        resolve(closed);
      };
      const check = () => {
        if (closedSocketIndexes.has(requestIndex)) finish(true);
      };
      socketCloseWaiters.add(check);
      timer = setTimeout(() => finish(false), timeoutMs);
    });
  };

  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    const portOffset = server.address().port - 8014;
    if (portOffset < 0 || portOffset > 57000) {
      throw new Error(`动态端口不能映射为开发端口偏移: ${portOffset}`);
    }
    const unit = buildTransientDevelopmentUnit({
      cwd: projectRoot,
      environment: {
        BOXTEAM_HOME: boxteamHome,
        BOXTEAM_DEV_PORT_OFFSET: String(portOffset),
        BOXTEAM_DEV_LOCAL_ONLY: "1",
      },
    });
    writeFileSync(unit.readyFile, JSON.stringify({ pid: 4141 }), { flag: "wx" });
    const expectedArguments = [
      "--user",
      "show",
      unit.serviceName,
      "--property=LoadState",
      "--property=ActiveState",
      "--property=SubState",
      "--property=MainPID",
      "--no-pager",
    ];
    spawnSyncSpy = spyOn(Bun, "spawnSync").mockImplementation((command) => {
      raw.systemctlCalls.push(command);
      if (JSON.stringify(command) !== JSON.stringify(["systemctl", ...expectedArguments])) {
        return {
          exitCode: 93,
          stdout: Buffer.alloc(0),
          stderr: Buffer.from("unexpected systemctl command"),
        };
      }
      return {
        exitCode: 0,
        stdout: Buffer.from(
          `LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=${managerPid}\n`,
        ),
        stderr: Buffer.alloc(0),
      };
    });

    try {
      await exercise({
        unit,
        raw,
        scheduleTimeout,
        waitForSocketClose,
        setManagerPid(pid) {
          managerPid = pid;
        },
        replaceFetch(replacement) {
          fetchSpy = spyOn(globalThis, "fetch").mockImplementation(replacement);
        },
      });
    } catch (error) {
      raw.error = error instanceof Error ? error.message : String(error);
      throw error;
    } finally {
      raw.closedSocketIndexes = [...closedSocketIndexes];
      writeFileSync(
        path.join(artifacts, "raw.json"),
        `${JSON.stringify(raw, null, 2)}\n`,
        { flag: "wx" },
      );
      writeFileSync(
        path.join(artifacts, "result.json"),
        `${JSON.stringify({ scenario, completed: raw.error === undefined, error: raw.error ?? null }, null, 2)}\n`,
        { flag: "wx" },
      );
    }
  } finally {
    for (const timer of scheduledTimers) clearTimeout(timer);
    socketCloseWaiters.clear();
    fetchSpy?.mockRestore();
    spawnSyncSpy?.mockRestore();
    server.closeAllConnections();
    await new Promise((resolve) => {
      if (!server.listening) {
        resolve();
        return;
      }
      server.close(resolve);
    });
  }
}

describe("transient systemd Gateway 就绪探针传输生命周期", () => {
  test("503响应连接在重试前关闭，200未结束正文在成功后及时释放", async () => {
    await withGatewayFixture(
      "retry-and-success-release",
      (_request, response, { requestIndex }) => {
        if (requestIndex === 1) {
          response.writeHead(503, { "content-type": "text/plain" });
          response.write("retry only after this stream closes");
          return;
        }
        response.writeHead(200, { "content-type": "text/plain" });
        response.write("healthy but deliberately unfinished");
      },
      async ({ unit, raw, waitForSocketClose }) => {
        await waitForGateway(unit, 2_000);
        expect(raw.events.indexOf("socket-close:1")).toBeGreaterThanOrEqual(0);
        expect(raw.events.indexOf("socket-close:1")).toBeLessThan(
          raw.events.indexOf("request:2"),
        );
        expect(await waitForSocketClose(2)).toBe(true);
        expect(raw.systemctlCalls.length).toBeGreaterThanOrEqual(3);
        for (const command of raw.systemctlCalls) {
          expect(command).toEqual([
            "systemctl",
            "--user",
            "show",
            unit.serviceName,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=MainPID",
            "--no-pager",
          ]);
        }
      },
    );
  });

  test("挂起请求在总预算内终止并释放连接", async () => {
    await withGatewayFixture(
      "hung-request-budget",
      () => {},
      async ({ unit, raw, waitForSocketClose }) => {
        const startedAt = performance.now();
        let error;
        try {
          await waitForGateway(unit, 180);
        } catch (caught) {
          error = caught;
        }
        const elapsedMs = performance.now() - startedAt;
        raw.observations = { elapsedMs, error: error?.message ?? null };
        expect(error).toBeInstanceOf(Error);
        expect(error.message).toContain("180ms 内未就绪");
        expect(elapsedMs).toBeLessThan(700);
        expect(await waitForSocketClose(1)).toBe(true);
      },
    );
  });

  test("超过期限才返回的健康响应会释放正文且不能通过", async () => {
    await withGatewayFixture(
      "late-success",
      () => {},
      async ({ unit, raw, replaceFetch, scheduleTimeout }) => {
        replaceFetch(async (_url, { signal }) => {
          await new Promise((resolve) => scheduleTimeout(resolve, 140));
          raw.signalAbortedWhenFetchReturned = signal.aborted;
          raw.lateFetchResolved = true;
          return new Response(
            new ReadableStream({
              cancel() {
                raw.lateResponseBodyCancelled = true;
              },
            }),
            { status: 200 },
          );
        });

        const startedAt = performance.now();
        let error;
        try {
          await waitForGateway(unit, 80);
        } catch (caught) {
          error = caught;
        }
        raw.observations = {
          elapsedMs: performance.now() - startedAt,
          error: error?.message ?? null,
        };
        expect(error).toBeInstanceOf(Error);
        expect(error.message).toContain("80ms 内未就绪");
        expect(error.message).toContain("健康响应超过就绪期限");
        expect(raw.lateFetchResolved).toBe(true);
        expect(raw.lateResponseBodyCancelled).toBe(true);
        expect(raw.signalAbortedWhenFetchReturned).toBe(true);
      },
    );
  });

  test("健康响应后仍核对manager PID和ready marker", async () => {
    await withGatewayFixture(
      "manager-replaced-during-health",
      (_request, response, { setManagerPid }) => {
        setManagerPid(5151);
        response.writeHead(200, { "content-type": "text/plain" });
        response.write("healthy from the replaced manager");
      },
      async ({ unit, raw, waitForSocketClose }) => {
        let error;
        try {
          await waitForGateway(unit, 400);
        } catch (caught) {
          error = caught;
        }
        raw.observations = { error: error?.message ?? null };
        expect(error).toBeInstanceOf(Error);
        expect(error.message).toContain("400ms 内未就绪");
        expect(error.message).toContain("development manager 已退出或更换");
        expect(await waitForSocketClose(1)).toBe(true);
      },
    );
  });
});
