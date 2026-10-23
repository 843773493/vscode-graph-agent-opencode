import { describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const outputRoot = path.resolve("out/tests/packaging/runtime/build-archive-downloads");
const runId = randomUUID();
const fixtureRunRoot = path.join(outputRoot, "workspace", runId);
const artifactsRunRoot = path.join(outputRoot, "artifacts", runId);
for (const kind of ["workspace", "artifacts"]) {
  mkdirSync(path.join(outputRoot, kind), { recursive: true });
  mkdirSync(path.join(outputRoot, kind, runId));
}
const observerPath = path.join(artifactsRunRoot, "observer.mjs");
writeFileSync(observerPath, `import { readFileSync } from "node:fs";
${observeBuildDownloads.toString()}
console.log(JSON.stringify(await observeBuildDownloads(...JSON.parse(readFileSync(process.argv[2], "utf8")))));
`, { flag: "wx" });

async function observeBuildDownloads(scriptUrl, mode, overrideRoots) {
  const { default: assert } = await import("node:assert/strict");
  const { existsSync, mkdirSync, readFileSync, writeFileSync } = await import("node:fs");
  const { default: path } = await import("node:path");
  const { createContext, SourceTextModule, SyntheticModule } = await import("node:vm");
  const projectRoot = process.cwd();
  for (const [relative, content] of [
    ["package.json", '{"version":"fixture"}\n'], ["bun.lock", "bun\n"],
    ["uv.lock", "uv\n"], ["pyproject.toml", "[project]\n"],
    ["app/source.py", "source\n"], ["configs/input.jsonc", "{}\n"],
    ["resources/input.txt", "resource\n"], ["src/shared/sse.js", "export {};\n"],
    ["src/clients/web/index.ts", "export {};\n"],
    ["src/workspace-services/input.mjs", "export {};\n"],
    ["packages/launcher/package.json", '{"name":"boxteam","version":"fixture"}\n'],
    ["packaging/runtime/node-package.json", "{}\n"],
    [`packages/runtime-${mode === "linux" ? "linux-x64" : "windows-x64"}/package.json`, "{}\n"],
    [`scripts/release/package-${mode === "linux" ? "linux-x64" : "windows-x64"}.mjs`, "export {};\n"],
    ["scripts/install/runtime-postinstall.mjs", "export {};\n"],
  ]) {
    const file = path.join(projectRoot, relative);
    mkdirSync(path.dirname(file), { recursive: true });
    writeFileSync(file, content);
  }
  process.env.BOXTEAM_PROJECT_ROOT = projectRoot;
  const target = mode === "linux" ? "linux-x64" : "windows-x64";
  const defaultDownloadRoot = path.join(projectRoot, "out/packaging", target, "downloads");
  const pythonDownloadRoot = overrideRoots ? path.join(projectRoot, "Python 缓存") : defaultDownloadRoot;
  const nodeDownloadRoot = overrideRoots ? path.join(projectRoot, "Node 缓存") : defaultDownloadRoot;
  const buildResultPath = path.join(projectRoot, "out/packaging", target, "build-result.json");
  mkdirSync(path.dirname(buildResultPath), { recursive: true });
  writeFileSync(buildResultPath, "stale success\n");
  const { computeBuildInputFingerprint } = await import(new URL("./build-input-fingerprint.mjs", scriptUrl));
  const initialFingerprint = computeBuildInputFingerprint({ projectRoot, targetPlatform: target });
  const calls = [];
  const stopBeforeExtract = new Error("fixture在首次解包前结束");
  const context = createContext({
    console, Buffer,
    process: {
      env: {
        BOXTEAM_PROJECT_ROOT: projectRoot,
        ...(overrideRoots ? {
          BOXTEAM_PYTHON_DOWNLOAD_ROOT: pythonDownloadRoot,
          BOXTEAM_NODE_DOWNLOAD_ROOT: nodeDownloadRoot,
        } : {}),
      },
      platform: mode === "windows-native" ? "win32" : "linux", arch: "x64",
      argv: mode === "windows-cross" ? ["--cross"] : [], cwd: () => projectRoot,
    },
  });
  // 原构建入口全文执行；仅替换下载与子进程边界，禁止真实下载、解包或安装。
  const module = new SourceTextModule(readFileSync(new URL(scriptUrl), "utf8"), { identifier: scriptUrl, context });
  await module.link(async (specifier) => {
    let exports;
    if (specifier === "./pinned-archive-download.mjs") {
      exports = Object.fromEntries([
        ["downloadPinnedLinuxPython", "linux-python"],
        ["downloadPinnedWindowsPython", "windows-python"],
        ["downloadPinnedWindowsNode", "windows-node"],
      ].map(([name, kind]) => [name, async (root) => {
        assert.equal(existsSync(buildResultPath), false, "下载开始前旧成功metadata必须已删除");
        calls.push([kind, root]);
        return path.join(root, `${kind}.archive`);
      }]));
    } else if (specifier === "./runtime-build-result.mjs") {
      const owner = await import(new URL(specifier, scriptUrl));
      exports = {
        ...owner,
        beginRuntimeBuild(args) {
          const fingerprint = owner.beginRuntimeBuild(args);
          calls.push(["source-snapshot", fingerprint]);
          return fingerprint;
        },
      };
    } else {
      exports = { ...await import(specifier.startsWith(".") ? new URL(specifier, scriptUrl).href : specifier) };
      if (specifier === "node:child_process") exports.spawnSync = (command, args, options) => {
        calls.push([command, args, options.cwd]);
        return { error: stopBeforeExtract };
      };
    }
    return new SyntheticModule(Object.keys(exports), function () {
      for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
    }, { context });
  });
  await assert.rejects(module.evaluate(), (error) => error === stopBeforeExtract);
  assert.equal(calls[0][0], "source-snapshot");
  assert.equal(calls[0][1], initialFingerprint);
  assert.equal(existsSync(buildResultPath), false, "失败构建不能复用旧成功metadata");
  const expectedPythonRoot = mode === "linux" ? defaultDownloadRoot : pythonDownloadRoot;
  const pythonKind = mode === "linux" ? "linux-python" : "windows-python";
  const expected = [[pythonKind, expectedPythonRoot]];
  if (mode !== "linux") expected.push(["windows-node", nodeDownloadRoot]);
  expected.push(["tar", ["-xzf", path.join(expectedPythonRoot, `${pythonKind}.archive`), "-C",
    path.join(projectRoot, "out/packaging", target, "stage", `runtime-${target}`, "package")], projectRoot]);
  // VM数组属于另一realm，序列化后核对实际下载顺序、目录和首次解包参数。
  assert.deepEqual(JSON.parse(JSON.stringify(calls.slice(1))), expected);
  return { downloads: expected.length - 1, snapshotMatchesInitial: true, oldMetadataRemoved: true, stoppedBeforeExtract: true };
}

describe("构建入口将下载目录交给唯一归档owner", () => {
  for (const [mode, overrideRoots] of [["linux", false], ["linux", true],
    ["windows-cross", false], ["windows-cross", true], ["windows-native", false], ["windows-native", true]]) {
    test(`${mode}/${overrideRoots ? "独立下载根" : "默认下载根"}`, () => {
      const scenario = overrideRoots ? "override-download-roots" : "default-download-root";
      const fixtureRoot = path.join(fixtureRunRoot, mode, scenario);
      const artifactsRoot = path.join(artifactsRunRoot, mode, scenario);
      for (const directory of [fixtureRoot, artifactsRoot]) {
        mkdirSync(path.dirname(directory), { recursive: true });
        mkdirSync(directory);
      }
      const scriptUrl = new URL(mode === "linux" ? "./build-linux-x64.mjs" : "./build-windows-x64.mjs", import.meta.url).href;
      const inputPath = path.join(artifactsRoot, "input.json");
      writeFileSync(inputPath, `${JSON.stringify([scriptUrl, mode, overrideRoots])}\n`, { flag: "wx" });
      const args = ["--experimental-vm-modules", observerPath, inputPath];
      const run = spawnSync("node", args, { cwd: fixtureRoot, timeout: 5000 });
      const stdout = run.stdout ?? Buffer.alloc(0);
      const stderr = run.stderr ?? Buffer.alloc(0);
      writeFileSync(path.join(artifactsRoot, "stdout.raw"), stdout, { flag: "wx" });
      writeFileSync(path.join(artifactsRoot, "stderr.raw"), stderr, { flag: "wx" });
      writeFileSync(path.join(artifactsRoot, "result.json"), `${JSON.stringify({
        command: ["node", ...args], cwd: fixtureRoot, timeout_ms: 5000,
        status: run.status, signal: run.signal,
        error: run.error ? { name: run.error.name, message: run.error.message, code: run.error.code ?? null } : null,
      }, null, 2)}\n`, { flag: "wx" });
      if (run.error || run.status !== 0) {
        throw new Error(`构建下载接线 ${mode}/${scenario} 失败: exit=${run.status} signal=${run.signal}；artifacts=${artifactsRoot}\n${stderr.toString("utf8")}`, { cause: run.error });
      }
      expect(JSON.parse(stdout.toString("utf8"))).toEqual({ downloads: mode === "linux" ? 1 : 2,
        snapshotMatchesInitial: true, oldMetadataRemoved: true, stoppedBeforeExtract: true });
    });
  }
});
