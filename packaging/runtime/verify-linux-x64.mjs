import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
} from "node:fs";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";

const projectRoot = path.resolve(
  process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd(),
);
const outputRoot = path.join(projectRoot, "out", "packaging", "linux-x64");
const tarballRoot = path.join(outputRoot, "tarballs");
const verificationRoot = path.join(outputRoot, "verification");
const installRoot = path.join(verificationRoot, "installed");
const relocatedRoot = path.join(verificationRoot, "relocated");
const boxteamHome = path.join(verificationRoot, "home");
const emptyPath = path.join(verificationRoot, "empty-path");
const gatewayUrl = "http://127.0.0.1:8014";
const headers = {};

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd ?? projectRoot,
    env: options.env ?? process.env,
    encoding: "utf8",
    stdio: options.capture ? "pipe" : "inherit",
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(
      `命令失败 (${String(result.status)}): ${command} ${args.join(" ")}\n` +
        `${result.stderr ?? ""}`,
    );
  }
  return result.stdout?.trim() ?? "";
}

function requiredTarball(prefix) {
  const result = readdirSync(tarballRoot)
    .filter((name) => name.startsWith(prefix) && name.endsWith(".tgz"))
    .map((name) => path.join(tarballRoot, name));
  if (result.length !== 1) {
    throw new Error(`期望一个 ${prefix} tarball，实际: ${result.join(", ")}`);
  }
  return result[0];
}

async function waitForGateway(child) {
  const deadline = Date.now() + 60_000;
  let lastError = null;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`安装版 Launcher 提前退出: ${child.exitCode}`);
    }
    try {
      const response = await fetch(`${gatewayUrl}/api/gateway/health`, {
        headers,
      });
      if (response.ok) return;
      lastError = new Error(`HTTP ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(
    `安装版 Gateway 未就绪: ${
      lastError instanceof Error ? lastError.message : String(lastError)
    }`,
  );
}

async function requestJson(pathname, options = {}) {
  const response = await fetch(`${gatewayUrl}${pathname}`, {
    ...options,
    headers: {
      ...headers,
      "Content-Type": "application/json",
      ...(options.headers ?? {}),
    },
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`${pathname} 返回 ${response.status}: ${text.slice(0, 500)}`);
  }
  return JSON.parse(text);
}

async function stopLauncher(child) {
  if (child.exitCode !== null) return;
  child.kill("SIGTERM");
  const exitedGracefully = await Promise.race([
    once(child, "exit").then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 45_000)),
  ]);
  if (exitedGracefully || child.exitCode !== null) return;
  if (process.platform === "win32") {
    child.kill("SIGKILL");
    return;
  }
  try {
    process.kill(-child.pid, "SIGKILL");
  } catch (error) {
    if (error?.code !== "ESRCH") throw error;
  }
}

async function verifyRunningProduct(child) {
  const rootResponse = await fetch(`${gatewayUrl}/`);
  if (!rootResponse.ok || !(await rootResponse.text()).includes("root")) {
    throw new Error("安装版 Gateway 未提供打包 Web UI");
  }
  const credentialResponse = await fetch(
    `${gatewayUrl}/api/gateway/auth/local-credential`,
    { headers: { "Sec-Fetch-Site": "same-origin" } },
  );
  if (!credentialResponse.ok) {
    throw new Error(`安装版 Gateway 本地凭据不可用: ${credentialResponse.status}`);
  }
  const credentialPayload = await credentialResponse.json();
  const localToken = credentialPayload.data?.token;
  if (typeof localToken !== "string" || !localToken) {
    throw new Error("安装版 Gateway 本地凭据响应非法");
  }
  headers["X-Local-Token"] = localToken;

  const workspacePayload = await requestJson("/api/gateway/workspaces");
  const workspace = workspacePayload.data?.items?.find((item) => item.managed);
  if (!workspace) {
    throw new Error("安装版 Gateway 未创建默认托管工作区");
  }

  const browserPayload = await requestJson(
    `/api/gateway/workspaces/${encodeURIComponent(
      workspace.workspace_id,
    )}/browser-manager/api/browsers`,
    {
      method: "POST",
      body: JSON.stringify({
        session_id: "ses_packaging_smoke",
        title: "Packaged Chromium Smoke",
        url: "data:text/html,<title>BoxTeam packaged browser</title>",
      }),
    },
  );
  if (browserPayload.data?.status !== "running") {
    throw new Error(`Browser Manager 未启动打包 Chromium: ${JSON.stringify(browserPayload)}`);
  }

  const restartPayload = await requestJson(
    `/api/gateway/workspaces/${encodeURIComponent(
      workspace.workspace_id,
    )}/runtime/restart-safe`,
    { method: "POST" },
  );
  if (restartPayload.data?.status !== "restarted") {
    throw new Error(`安全重启后端失败: ${JSON.stringify(restartPayload)}`);
  }

  child.kill("SIGTERM");
  const [exitCode, signal] = await Promise.race([
    once(child, "exit"),
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error("Launcher 关闭超时")), 45_000),
    ),
  ]);
  if (![0, 128].includes(exitCode) && signal !== "SIGTERM") {
    throw new Error(
      `Launcher 关闭结果异常: exit=${String(exitCode)} signal=${String(signal)}`,
    );
  }
}

async function main() {
  const mainTarball = requiredTarball("boxteam-0.1.0");
  const runtimeTarball = requiredTarball("boxteam-runtime-linux-x64-0.1.0");
  const nodeExecutable = run("node", ["--print", "process.execPath"], {
    capture: true,
  });

  rmSync(verificationRoot, { recursive: true, force: true });
  mkdirSync(installRoot, { recursive: true });
  mkdirSync(emptyPath, { recursive: true });
  run(
    "npm",
    [
      "install",
      "--ignore-scripts",
      "--no-audit",
      "--no-fund",
      "--prefix",
      installRoot,
      mainTarball,
      runtimeTarball,
    ],
  );
  renameSync(installRoot, relocatedRoot);

  const launcherEntry = path.join(
    relocatedRoot,
    "node_modules",
    "boxteam",
    "bin",
    "boxteam.mjs",
  );
  if (!existsSync(launcherEntry)) {
    throw new Error(`relocation 后缺少 Launcher: ${launcherEntry}`);
  }
  const environment = {
    ...process.env,
    BOXTEAM_HOME: boxteamHome,
    PATH: emptyPath,
  };
  const doctor = run(nodeExecutable, [launcherEntry, "doctor", "--json"], {
    cwd: relocatedRoot,
    env: environment,
    capture: true,
  });
  const doctorPayload = JSON.parse(doctor);
  if (doctorPayload.distribution !== "npm") {
    throw new Error(`doctor 未使用 npm runtime: ${doctor}`);
  }

  const child = spawn(nodeExecutable, [launcherEntry, "--no-open"], {
    cwd: relocatedRoot,
    env: environment,
    stdio: "inherit",
    // 隔离验证进程组，避免测试清理信号被外层命令执行器解释为自身退出。
    detached: process.platform !== "win32",
  });
  try {
    await waitForGateway(child);
    await verifyRunningProduct(child);
  } finally {
    await stopLauncher(child);
  }

  const configPath = path.join(boxteamHome, "config", "boxteam.jsonc");
  if (!existsSync(configPath)) {
    throw new Error(`首次启动未生成用户配置: ${configPath}`);
  }
  const manifest = JSON.parse(
    readFileSync(
      path.join(
        relocatedRoot,
        "node_modules",
        "@boxteam",
        "runtime-linux-x64",
        "runtime-manifest.json",
      ),
      "utf8",
    ),
  );
  if (path.isAbsolute(manifest.python_executable)) {
    throw new Error("runtime manifest 不得记录构建机 Python 绝对路径");
  }
  process.stdout.write(`relocation 验证通过: ${relocatedRoot}\n`);
}

await main();
