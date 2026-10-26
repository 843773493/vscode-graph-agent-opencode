import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { spawnSync } from "node:child_process";
import { once } from "node:events";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
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
import { spawnDetachedProcess, terminateDetachedProcess } from "../detached-process.mjs";

const contract = loadRuntimeLogContract();
const { max_bytes: maxLogBytes } = contract.rotation;

const serviceLogFixtureRunRoot = path.join(
  process.cwd(), "out/tests/packages/launcher/src/logging/service-log/workspace", randomUUID(),
);

function temporaryRoot() {
  const root = path.join(serviceLogFixtureRunRoot, randomUUID());
  mkdirSync(root, { recursive: true });
  return root;
}

describe("service log", async () => {
  test("Python 和 JS 共用全部角色路径样本", async () => {
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

  test("发行资源按显式 application root 定位，不依赖 Launcher 安装目录", async () => {
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

  test("detached 的 Gateway 输出只写 Gateway 文件，覆盖只作用于 Launcher", async () => {
    const home = temporaryRoot();
    const outerPath = path.join(home, "custom-launcher.log");
    const script = `
      import { openServiceLog } from ${JSON.stringify(import.meta.url.replace(".test.mjs", ".mjs"))};
      import { loadRuntimeLogContract } from ${JSON.stringify(new URL("../../../../src/shared/serverRuntimeLogging.mjs", import.meta.url).href)};
      const options = { contract: loadRuntimeLogContract(), boxteamHome: ${JSON.stringify(home)} };
      const launcher = await openServiceLog(options);
      const gateway = await openServiceLog({ ...options, role: 'gateway' });
      launcher.stdout.write('launcher only\\n');
      gateway.stdout.write('gateway 中文\\n'); gateway.stderr.write('gateway error\\n');
      gateway.close(); launcher.close();
    `;
    const child = await spawnDetachedProcess({ command: "node", args: ["--input-type=module", "--eval", script],
      cwd: process.cwd(), environment: { ...process.env, [SERVICE_LOG_PATH_ENV]: outerPath }, logPath: outerPath, contract });
    try {
      expect(await once(child, "exit", { signal: AbortSignal.timeout(5000) })).toEqual([0, null]);
      expect(readFileSync(outerPath, "utf8")).toBe("launcher only\n");
      expect(readFileSync(contract.pathFor(contract.rootFor(home), "gateway"), "utf8")).toBe("gateway 中文\ngateway error\n");
    } finally {
      if (child.exitCode === null && child.signalCode === null) terminateDetachedProcess(child.pid);
    }
  });

  test("默认写入 BOXTEAM_HOME 的 Launcher 日志目录", async () => {
    const boxteamHome = temporaryRoot();

    expect(resolveServiceLogPath(boxteamHome, {}, contract)).toBe(
      path.join(boxteamHome, "state", "logs", "launcher", "services.log"),
    );
  });

  test("同时写入持久化日志和前台输出", async () => {
    const boxteamHome = temporaryRoot();
    const stdout = [];
    const stderr = [];
    const serviceLog = await openServiceLog({ contract,
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

  test("捕获标记缺少真实去向证明时明确拒绝", async () => {
    const boxteamHome = temporaryRoot();
    const logPath = path.join(boxteamHome, "artifacts", "services.log");
    const stdout = { write() {} };
    const stderr = { write() {} };
    await expect(openServiceLog({ contract,
      boxteamHome,
      environment: {
        [SERVICE_LOG_PATH_ENV]: logPath,
        [SERVICE_LOG_CAPTURED_ENV]: "1",
      },
      stdout,
      stderr,
    })).rejects.toThrow("外层日志捕获缺少冻结去向证明");
    expect(existsSync(logPath)).toBe(false);
  });

  test("启动时轮转超过上限的旧日志", async () => {
    const boxteamHome = temporaryRoot();
    const logPath = resolveServiceLogPath(boxteamHome, {}, contract);
    mkdirSync(path.dirname(logPath), { recursive: true });
    writeFileSync(logPath, "");
    truncateSync(logPath, maxLogBytes);

    const serviceLog = await openServiceLog({ contract,
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

  test("活动文件达到阈值时拒绝另一 owner，关闭后才允许轮转", async () => {
    const home = temporaryRoot();
    const options = { boxteamHome: home, environment: {}, contract, stdout: { write() {} }, stderr: { write() {} } };
    const log = await openServiceLog(options);
    const inode = statSync(log.path).ino;
    try {
      truncateSync(log.path, maxLogBytes);
      for (const index of [1, 2, 3]) writeFileSync(`${log.path}.${index}`, `archive-${index}`);
      await expect(openServiceLog(options)).rejects.toThrow("活动写入者");
      expect(statSync(log.path).ino).toBe(inode);
      for (const index of [1, 2, 3]) expect(readFileSync(`${log.path}.${index}`, "utf8")).toBe(`archive-${index}`);
    } finally { log.close(); }
    const next = await openServiceLog(options);
    next.close();
    expect(statSync(`${log.path}.1`).size).toBe(maxLogBytes);
    expect(readFileSync(`${log.path}.2`, "utf8")).toBe("archive-1");
  });

  test("不同 home 的显式路径覆盖不能创建双写 owner", async () => {
    const firstHome = temporaryRoot();
    const secondHome = temporaryRoot();
    const target = path.join(temporaryRoot(), "shared.log");
    const options = { contract, environment: { [SERVICE_LOG_PATH_ENV]: target }, stdout: { write() {} }, stderr: { write() {} } };
    const log = await openServiceLog({ ...options, boxteamHome: firstHome });
    try { await expect(openServiceLog({ ...options, boxteamHome: secondHome })).rejects.toThrow("活动写入者"); }
    finally { log.close(); }
  });

  test.skipIf(process.platform === "win32")("外部替换活跃日志不能绕过文件身份核验", async () => {
    const options = { contract, boxteamHome: temporaryRoot(), environment: {}, stdout: { write() {} }, stderr: { write() {} } };
    const log = await openServiceLog(options);
    try {
      renameSync(log.path, `${log.path}.moved`);
      writeFileSync(log.path, "replacement");
      await expect(openServiceLog(options)).rejects.toThrow("原日志文件身份已变化");
      log.stdout.write("original writer\n");
      expect(readFileSync(`${log.path}.moved`, "utf8")).toBe("original writer\n");
      expect(readFileSync(log.path, "utf8")).toBe("replacement");
      expect(existsSync(`${log.path}.1`)).toBe(false);
    } finally { log.close(); }
  });

  test("Python 与 Node 对同一路径的活动写入证明一致", async () => {
    const home = temporaryRoot();
    const options = { contract, boxteamHome: home, environment: {}, stdout: { write() {} }, stderr: { write() {} } };
    const log = await openServiceLog(options);
    const python = process.env.BOXTEAM_PYTHON_BIN ?? path.resolve(".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
    const script = `from pathlib import Path; from app.gateway.runtime.process_logs import ProcessLogStore; ProcessLogStore(Path(${JSON.stringify(contract.rootFor(home))})).open('launcher').close()`;
    let result;
    try {
      result = spawnSync(python, ["-c", script], { encoding: "utf8", timeout: 10000 });
      expect(result.status).not.toBe(0);
      expect(result.stderr).toContain("活动写入者");
    } finally { log.close(); }
    result = spawnSync(python, ["-c", script], { encoding: "utf8", timeout: 10000 });
    expect(result.status).toBe(0);
    const next = await openServiceLog(options);
    next.close();
  });

  test("detached 根退出后，后代持有输出时仍不能轮转", async () => {
    const home = temporaryRoot();
    const logPath = resolveServiceLogPath(home, {}, contract);
    const marker = path.join(home, "descendant.pid");
    const stop = path.join(home, "descendant.stop");
    const descendant = `import { writeFileSync, existsSync } from 'node:fs';
      writeFileSync(${JSON.stringify(marker)}, String(process.pid));
      setInterval(() => { if (existsSync(${JSON.stringify(stop)})) process.exit(0); }, 10);
      setTimeout(() => process.exit(0), 15000);`;
    // Windows 后代使用独立控制台，避免根控制台关闭事件先于 Node 初始化终止它。
    const script = `import { spawn } from 'node:child_process'; spawn(process.execPath, ['--input-type=module','--eval',${JSON.stringify(descendant)}], { stdio: 'inherit', detached: process.platform === 'win32', windowsHide: true }).unref();`;
    const child = await spawnDetachedProcess({ command: "node", args: ["--input-type=module", "--eval", script],
      cwd: process.cwd(), environment: process.env, logPath, contract });
    const options = { contract, boxteamHome: home, environment: {}, stdout: { write() {} }, stderr: { write() {} } };
    try {
      expect(await once(child, "exit", { signal: AbortSignal.timeout(5000) })).toEqual([0, null]);
      const deadline = performance.now() + 5000;
      while (!existsSync(marker) && performance.now() < deadline) await Bun.sleep(10);
      expect(existsSync(marker)).toBe(true);
      await expect(openServiceLog(options)).rejects.toThrow("活动写入者");
    } finally {
      // 后代自行关闭继承句柄，随后实际重开文件核验；外层 Job/进程保护兜住中断。
      writeFileSync(stop, "stop");
    }
    let reopened;
    const deadline = performance.now() + 5000;
    while (performance.now() < deadline) {
      try { reopened = await openServiceLog(options); break; }
      catch (error) {
        if (!error.message.includes("活动写入者")) throw error;
        await Bun.sleep(10);
      }
    }
    expect(reopened).toBeDefined();
    reopened.close();
  }, 15000);

  test("detached 子进程不能用被篡改的捕获证明接管文件", async () => {
    const home = temporaryRoot();
    const logPath = resolveServiceLogPath(home, {}, contract);
    const script = `
      import { openServiceLog } from ${JSON.stringify(import.meta.url.replace(".test.mjs", ".mjs"))};
      import { loadRuntimeLogContract } from ${JSON.stringify(new URL("../../../../src/shared/serverRuntimeLogging.mjs", import.meta.url).href)};
      const sink = JSON.parse(process.env.BOXTEAM_RUNTIME_LOG_SINK); sink.inode = 'changed';
      process.env.BOXTEAM_RUNTIME_LOG_SINK = JSON.stringify(sink);
      await openServiceLog({ contract: loadRuntimeLogContract(), boxteamHome: ${JSON.stringify(home)} });
    `;
    const child = await spawnDetachedProcess({ command: "node", args: ["--input-type=module", "--eval", script],
      cwd: process.cwd(), environment: process.env, logPath, contract });
    try {
      const [code] = await once(child, "exit", { signal: AbortSignal.timeout(5000) });
      expect(code).not.toBe(0);
      expect(readFileSync(logPath, "utf8")).toContain("实际输出描述符不匹配");
      expect(existsSync(`${logPath}.1`)).toBe(false);
    } finally {
      if (child.exitCode === null && child.signalCode === null) terminateDetachedProcess(child.pid);
    }
  });
});
