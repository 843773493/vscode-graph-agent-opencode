import { afterEach, describe, expect, test } from "bun:test";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  openServiceLog,
  resolveServiceLogPath,
  SERVICE_LOG_CAPTURED_ENV,
  SERVICE_LOG_PATH_ENV,
} from "./service-log.mjs";

const temporaryRoots = [];

function temporaryRoot() {
  const root = mkdtempSync(path.join(os.tmpdir(), "boxteam-service-log-"));
  temporaryRoots.push(root);
  return root;
}

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) {
    rmSync(root, { recursive: true, force: true });
  }
});

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
});
