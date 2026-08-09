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
const outputRoot = path.join(projectRoot, "out", "packaging", "windows-x64");
const tarballRoot = path.join(outputRoot, "tarballs");
const releaseAssetRoot = path.join(outputRoot, "release-assets");
const verificationRoot = path.join(outputRoot, "verification");
const installRoot = path.join(verificationRoot, "installed");
const relocatedRoot = path.join(verificationRoot, "relocated");
const standaloneExtractRoot = path.join(verificationRoot, "standalone");
const boxteamHome = path.join(verificationRoot, "home");
const standaloneBoxteamHome = path.join(verificationRoot, "standalone-home");
const emptyPath = path.join(verificationRoot, "empty-path");
const gatewayUrl = "http://127.0.0.1:8114";
// TODO: Windows 打包 Python 首次导入依赖较慢，验证等待窗口需覆盖 Launcher 的冷启动上限。
const gatewayReadyTimeoutMs = 180_000;
const headers = {};
const npmCommand = process.platform === "win32" ? "npm.cmd" : "npm";

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd ?? projectRoot,
    env: options.env ?? process.env,
    encoding: "utf8",
    shell: options.shell ?? false,
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

function stopWindowsProcessTree(pid) {
  const result = spawnSync("taskkill.exe", ["/T", "/F", "/PID", String(pid)], {
    encoding: "utf8",
    stdio: "pipe",
  });
  if (result.status === 0) return;
  const message = `${result.stdout ?? ""}\n${result.stderr ?? ""}`;
  if (/not found|no running instance/i.test(message)) return;
  throw new Error(
    `命令失败 (${String(result.status)}): taskkill.exe /T /F /PID ${String(pid)}\n${message}`,
  );
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

function requiredReleaseAsset(prefix) {
  const result = readdirSync(releaseAssetRoot)
    .filter((name) => name.startsWith(prefix) && name.endsWith(".tgz"))
    .map((name) => path.join(releaseAssetRoot, name));
  if (result.length !== 1) {
    throw new Error(
      `期望一个 ${prefix} release asset，实际: ${result.join(", ")}`,
    );
  }
  return result[0];
}

function requiredStandaloneArchive() {
  const result = readdirSync(path.join(outputRoot, "standalone"))
    .filter((name) => name.startsWith("boxteam-windows-x64-") && name.endsWith(".zip"))
    .map((name) => path.join(outputRoot, "standalone", name));
  if (result.length !== 1) {
    throw new Error(`期望一个 Windows 便携版 ZIP，实际: ${result.join(", ")}`);
  }
  return result[0];
}

function signalChild(child, signal) {
  try {
    child.kill(signal);
  } catch (error) {
    if (process.platform !== "win32" || error?.code !== "ENOSYS") {
      throw error;
    }
    // TODO: Bun 的 Windows child_process 当前不实现信号发送，使用精确 PID
    // 的 taskkill 兜底，避免验证失败时遗留 Gateway 进程。
    stopWindowsProcessTree(child.pid);
  }
}

async function waitForGateway(child) {
  const deadline = Date.now() + gatewayReadyTimeoutMs;
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

async function waitForGatewayStopped() {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    try {
      await fetch(`${gatewayUrl}/api/gateway/health`, { headers });
    } catch {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error("Windows Launcher 结束后 Gateway 仍在监听");
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
    throw new Error(
      `${pathname} 返回 ${response.status}: ${text.slice(0, 500)}`,
    );
  }
  return JSON.parse(text);
}

async function stopLauncher(child) {
  if (child.exitCode !== null) return;
  signalChild(child, process.platform === "win32" ? "SIGBREAK" : "SIGTERM");
  const exitedGracefully = await Promise.race([
    once(child, "exit").then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 45_000)),
  ]);
  if (exitedGracefully || child.exitCode !== null) return;
  if (process.platform === "win32") {
    stopWindowsProcessTree(child.pid);
    return;
  }
  try {
    process.kill(-child.pid, "SIGKILL");
  } catch (error) {
    if (error?.code !== "ESRCH") throw error;
  }
}

async function verifyRunningProduct(
  child,
  readLauncherOutput,
  { forceWindowsTreeShutdown = false } = {},
) {
  const rootResponse = await fetch(`${gatewayUrl}/`);
  if (!rootResponse.ok || !(await rootResponse.text()).includes("root")) {
    throw new Error("安装版 Gateway 未提供打包 Web UI");
  }
  const credentialResponse = await fetch(
    `${gatewayUrl}/api/gateway/auth/local-credential`,
    { headers: { "Sec-Fetch-Site": "same-origin" } },
  );
  if (!credentialResponse.ok) {
    throw new Error(
      `安装版 Gateway 本地凭据不可用: ${credentialResponse.status}`,
    );
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
        session_id: "ses_packaging_smoke_windows",
        title: "Packaged Chromium Smoke Windows",
        url: "data:text/html,<title>BoxTeam packaged browser Windows</title>",
      }),
    },
  );
  if (browserPayload.data?.status !== "running") {
    throw new Error(
      `Browser Manager 未启动打包 Chromium: ${JSON.stringify(browserPayload)}`,
    );
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

  const windowsShutdown = process.platform === "win32";
  const forcedWindowsTreeShutdown = forceWindowsTreeShutdown && windowsShutdown;
  if (forcedWindowsTreeShutdown) {
    // Windows 原生启动器还会托管 Node/Python 子进程，必须连同进程树一起结束。
    stopWindowsProcessTree(child.pid);
  } else {
    signalChild(child, process.platform === "win32" ? "SIGBREAK" : "SIGINT");
  }
  const [exitCode, signal] = await Promise.race([
    once(child, "exit"),
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error("Launcher 关闭超时")), 45_000),
    ),
  ]);
  // TODO: Windows 的 Node/命令外壳结束码可能是 1 或带有信号标记；以 Gateway
  // 端口确实关闭作为清理成功条件，避免把已清理完成的进程树误报为失败。
  if (!windowsShutdown && (exitCode !== 0 || signal !== null)) {
    throw new Error(
      `Launcher 关闭结果异常: exit=${String(exitCode)} signal=${String(signal)}`,
    );
  }
  if (windowsShutdown) await waitForGatewayStopped();
  const launcherOutput = readLauncherOutput();
  if (/Traceback|KeyboardInterrupt|CancelledError/.test(launcherOutput)) {
    throw new Error(`Launcher 关闭输出包含异常堆栈:\n${launcherOutput}`);
  }
}

async function main() {
  if (process.platform !== "win32" || process.arch !== "x64") {
    throw new Error(
      `windows-x64 验证器不支持当前平台: ${process.platform}-${process.arch}`,
    );
  }
  const mainTarball = requiredTarball("boxteam-0.1.0");
  const runtimeTarball = requiredTarball("boxteam-runtime-windows-x64-0.1.0");
  const releaseAssetPath = requiredReleaseAsset(
    "boxteam-runtime-windows-x64-0.1.0",
  );
  const standaloneArchivePath = requiredStandaloneArchive();
  const nodeExecutable = run("node", ["--print", "process.execPath"], {
    capture: true,
  });

  rmSync(verificationRoot, { recursive: true, force: true });
  mkdirSync(installRoot, { recursive: true });
  mkdirSync(emptyPath, { recursive: true });
  run(
    npmCommand,
    [
      "install",
      "--no-audit",
      "--no-fund",
      "--prefix",
      installRoot,
      "--dangerously-allow-all-scripts",
      mainTarball,
      runtimeTarball,
    ],
    {
      env: {
        ...process.env,
        BOXTEAM_RUNTIME_ASSET_PATH: releaseAssetPath,
      },
      shell: true,
    },
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
    // 清空 PATH，证明启动依赖的是 npm 包内 Python 和 Chromium。
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
    stdio: ["ignore", "pipe", "pipe"],
  });
  let launcherOutput = "";
  for (const stream of [child.stdout, child.stderr]) {
    stream?.on("data", (chunk) => {
      const text = String(chunk);
      launcherOutput += text;
      process.stdout.write(text);
    });
  }
  try {
    await waitForGateway(child);
    await verifyRunningProduct(child, () => launcherOutput);
  } finally {
    await stopLauncher(child);
  }

  for (const name of [
    "gateway.jsonc",
    "gateway_schema.jsonc",
    "workspace.jsonc",
    "workspace_schema.jsonc",
  ]) {
    const configPath = path.join(boxteamHome, "config", name);
    if (!existsSync(configPath)) {
      throw new Error(`首次启动未生成配置资源: ${configPath}`);
    }
  }
  const manifest = JSON.parse(
    readFileSync(
      path.join(
        relocatedRoot,
        "node_modules",
        "@boxteam",
        "runtime-windows-x64",
        "runtime-manifest.json",
      ),
      "utf8",
    ),
  );
  const installedRuntimeRoot = path.join(
    relocatedRoot,
    "node_modules",
    "@boxteam",
    "runtime-windows-x64",
  );
  for (const resource of [
    manifest.python_executable,
    ...Object.values(manifest.config_resources),
    manifest.skill_resources,
  ]) {
    if (path.isAbsolute(resource)) {
      throw new Error(`runtime manifest 不得记录构建机绝对路径: ${resource}`);
    }
  }
  if (!existsSync(path.resolve(installedRuntimeRoot, manifest.skill_resources))) {
    throw new Error(
      `安装包缺少共享 Skill 资源: ${manifest.skill_resources}`,
    );
  }
  process.stdout.write(`Windows relocation 验证通过: ${relocatedRoot}\n`);

  mkdirSync(standaloneExtractRoot, { recursive: true });
  run("tar", ["-xf", standaloneArchivePath, "-C", standaloneExtractRoot]);
  const [standaloneDirectory] = readdirSync(standaloneExtractRoot, {
    withFileTypes: true,
  }).filter((entry) => entry.isDirectory());
  if (!standaloneDirectory) {
    throw new Error(`便携版 ZIP 缺少顶层目录: ${standaloneArchivePath}`);
  }
  const standalonePackageRoot = path.join(
    standaloneExtractRoot,
    standaloneDirectory.name,
  );
  const standaloneLauncher = path.join(standalonePackageRoot, "BoxTeam.exe");
  const standaloneDoctorLauncher = path.join(
    standalonePackageRoot,
    "BoxTeamDoctor.exe",
  );
  const standaloneNode = path.join(
    standalonePackageRoot,
    "runtime",
    "node",
    "node.exe",
  );
  if (
    !existsSync(standaloneLauncher) ||
    !existsSync(standaloneDoctorLauncher) ||
    !existsSync(standaloneNode)
  ) {
    throw new Error(
      `便携版缺少启动文件: launcher=${standaloneLauncher} doctor=${standaloneDoctorLauncher} node=${standaloneNode}`,
    );
  }
  const standaloneEnvironment = {
    ...process.env,
    BOXTEAM_HOME: standaloneBoxteamHome,
    // 清空 PATH，证明便携版使用自己携带的 Node、Python 和 Chromium。
    PATH: emptyPath,
  };
  const standaloneDoctor = run(
    standaloneDoctorLauncher,
    ["--json"],
    {
      cwd: standalonePackageRoot,
      env: standaloneEnvironment,
      capture: true,
    },
  );
  const standaloneDoctorPayload = JSON.parse(standaloneDoctor);
  if (standaloneDoctorPayload.distribution !== "standalone") {
    throw new Error(`便携版 doctor 未使用 standalone runtime: ${standaloneDoctor}`);
  }
  if (
    standaloneDoctorPayload.node?.source !== "bundled" ||
    standaloneDoctorPayload.node?.executable !== standaloneNode ||
    standaloneDoctorPayload.node?.exists !== true
  ) {
    throw new Error(`便携版未使用携带的 Node: ${standaloneDoctor}`);
  }

  const standaloneChild = spawn(
    standaloneLauncher,
    ["--no-open"],
    {
      cwd: standalonePackageRoot,
      env: standaloneEnvironment,
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let standaloneOutput = "";
  for (const stream of [standaloneChild.stdout, standaloneChild.stderr]) {
    stream?.on("data", (chunk) => {
      const text = String(chunk);
      standaloneOutput += text;
      process.stdout.write(text);
    });
  }
  try {
    await waitForGateway(standaloneChild);
    await verifyRunningProduct(
      standaloneChild,
      () => standaloneOutput,
      { forceWindowsTreeShutdown: true },
    );
  } finally {
    await stopLauncher(standaloneChild);
  }
  for (const name of [
    "gateway.jsonc",
    "gateway_schema.jsonc",
    "workspace.jsonc",
    "workspace_schema.jsonc",
  ]) {
    const configPath = path.join(standaloneBoxteamHome, "config", name);
    if (!existsSync(configPath)) {
      throw new Error(`便携版首次启动未生成配置资源: ${configPath}`);
    }
  }
  process.stdout.write(`Windows 便携版验证通过: ${standalonePackageRoot}\n`);
}

await main();
