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
  SERVICE_LOG_PATH_ENV,
} from "./service-log.mjs";
import { loadRuntimeLogContract } from "../../../../src/shared/serverRuntimeLogging.mjs";

const contract = loadRuntimeLogContract();
const { max_bytes: maxLogBytes } = contract.rotation;

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
  test("Python 和 JS 共用全部角色路径样本", () => {
    const home = temporaryRoot();
    const samples = JSON.parse(readFileSync(path.resolve("resources/logging/path-samples.json"), "utf8"));
    for (const sample of samples) {
      expect(contract.pathFor(contract.rootFor(home), sample.role, sample.instance)).toBe(
        path.resolve(home, sample.path),
      );
    }
    for (const instance of ["", "../outside", "C:\\outside", "/etc", "a\\b", ".", "中文"]) {
      expect(() => contract.pathFor(home, "backend", instance)).toThrow("进程日志实例");
    }
  });

  test("发行资源按显式 application root 定位，不依赖 Launcher 安装目录", () => {
    const applicationRoot = temporaryRoot();
    const resources = path.join(applicationRoot, "resources/logging");
    mkdirSync(resources, { recursive: true });
    const contract = readFileSync(path.resolve("resources/logging/runtime-contract.json"));
    writeFileSync(path.join(resources, "runtime-contract.json"), contract);
    const home = temporaryRoot();
    expect(resolveServiceLogPath(home, {}, loadRuntimeLogContract(applicationRoot))).toBe(
      path.join(home, "state/logs/launcher/services.log"),
    );
    expect(() => loadRuntimeLogContract(path.join(applicationRoot, "missing"))).toThrow();
  });

  test("detached 的 Gateway 输出只写 Gateway 文件，覆盖只作用于 Launcher", () => {
    const home = temporaryRoot();
    const outerPath = path.join(home, "custom-launcher.log");
    const environment = { [SERVICE_LOG_PATH_ENV]: outerPath, [SERVICE_LOG_CAPTURED_ENV]: "1" };
    const captured = [];
    const primary = { write: (value) => captured.push(value) };
    const launcher = openServiceLog({ contract, boxteamHome: home, environment, stdout: primary, stderr: primary });
    const gateway = openServiceLog({ contract, boxteamHome: home, environment, role: "gateway", stdout: primary, stderr: primary });
    try {
      launcher.stdout.write("launcher only\n");
      gateway.stdout.write("gateway 中文\n");
      gateway.stderr.write("gateway error\n");
      expect(launcher.path).toBe(outerPath);
      expect(gateway.path).toBe(path.join(home, "state/logs/gateway/gateway.log"));
      expect(captured).toEqual(["launcher only\n"]);
      expect(readFileSync(gateway.path, "utf8")).toBe("gateway 中文\ngateway error\n");
    } finally {
      gateway.close();
      launcher.close();
    }
  });

  test("默认写入 BOXTEAM_HOME 的 Launcher 日志目录", () => {
    const boxteamHome = temporaryRoot();

    expect(resolveServiceLogPath(boxteamHome, {}, contract)).toBe(
      path.join(boxteamHome, "state", "logs", "launcher", "services.log"),
    );
  });

  test("同时写入持久化日志和前台输出", () => {
    const boxteamHome = temporaryRoot();
    const stdout = [];
    const stderr = [];
    const serviceLog = openServiceLog({ contract,
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
    const serviceLog = openServiceLog({ contract,
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
    const logPath = resolveServiceLogPath(boxteamHome, {}, contract);
    mkdirSync(path.dirname(logPath), { recursive: true });
    writeFileSync(logPath, "");
    truncateSync(logPath, maxLogBytes);

    const serviceLog = openServiceLog({ contract,
      boxteamHome,
      environment: {},
      stdout: { write() {} },
      stderr: { write() {} },
    });
    serviceLog.stdout.write("new\n");
    serviceLog.close();

    expect(readFileSync(logPath, "utf8")).toBe("new\n");
    expect(statSync(`${logPath}.1`).size).toBe(maxLogBytes);
  });
});
