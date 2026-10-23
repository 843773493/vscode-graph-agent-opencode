import { afterAll, describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  statSync,
  truncateSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";

import {
  openServiceLog,
  resolveServiceLogPath,
  SERVICE_LOG_CAPTURED_ENV,
  SERVICE_LOG_MAX_BYTES,
  SERVICE_LOG_PATH_ENV,
} from "./service-log.mjs";

const serviceLogFixtureRunRoot = path.join(
  process.cwd(), "out/tests/packages/launcher/src/logging/service-log/workspace", randomUUID(),
);

// 文件级owner先登记清理，准备或断言失败也只回收本次运行。
afterAll(() => rmSync(serviceLogFixtureRunRoot, { recursive: true, force: true }));

function temporaryRoot() {
  const root = path.join(serviceLogFixtureRunRoot, randomUUID());
  mkdirSync(root, { recursive: true });
  return root;
}

describe("service log", () => {
  test("默认写入 BOXTEAM_HOME 的 Launcher 日志目录", () => {
    const boxteamHome = temporaryRoot();

    expect(resolveServiceLogPath(boxteamHome, {})).toBe(
      path.join(boxteamHome, "state", "launcher", "logs", "services.log"),
    );
  });

  test("同时写入持久化日志和前台输出", () => {
    const boxteamHome = temporaryRoot();
    const stdout = [];
    const stderr = [];
    const serviceLog = openServiceLog({
      boxteamHome,
      environment: {},
      stdout: { write: (value) => stdout.push(String(value)) },
      stderr: { write: (value) => stderr.push(String(value)) },
    });

    serviceLog.stdout.write("launcher ready\n");
    serviceLog.stderr.write("gateway warning\n");
    serviceLog.close();

    expect(stdout).toEqual(["launcher ready\n"]);
    expect(stderr).toEqual(["gateway warning\n"]);
    expect(readFileSync(serviceLog.path, "utf8")).toBe(
      "launcher ready\ngateway warning\n",
    );
    if (process.platform !== "win32") {
      expect(statSync(serviceLog.path).mode & 0o777).toBe(0o600);
    }
  });

  test("外层已经接管输出时不重复打开日志", () => {
    const boxteamHome = temporaryRoot();
    const logPath = path.join(boxteamHome, "artifacts", "services.log");
    const stdout = { write() {} };
    const stderr = { write() {} };
    const serviceLog = openServiceLog({
      boxteamHome,
      environment: {
        [SERVICE_LOG_PATH_ENV]: logPath,
        [SERVICE_LOG_CAPTURED_ENV]: "1",
      },
      stdout,
      stderr,
    });

    expect(serviceLog.path).toBe(logPath);
    expect(serviceLog.stdout).toBe(stdout);
    expect(serviceLog.stderr).toBe(stderr);
    serviceLog.close();
    expect(existsSync(logPath)).toBe(false);
  });

  test("启动时轮转超过上限的旧日志", () => {
    const boxteamHome = temporaryRoot();
    const logPath = resolveServiceLogPath(boxteamHome, {});
    mkdirSync(path.dirname(logPath), { recursive: true });
    writeFileSync(logPath, "");
    truncateSync(logPath, SERVICE_LOG_MAX_BYTES);

    const serviceLog = openServiceLog({
      boxteamHome,
      environment: {},
      stdout: { write() {} },
      stderr: { write() {} },
    });
    serviceLog.stdout.write("new\n");
    serviceLog.close();

    expect(readFileSync(logPath, "utf8")).toBe("new\n");
    expect(statSync(`${logPath}.1`).size).toBe(SERVICE_LOG_MAX_BYTES);
  });
});
