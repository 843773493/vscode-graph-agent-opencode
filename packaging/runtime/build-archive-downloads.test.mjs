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

async function observeBuildDownloads(scriptUrl, mode, overrideRoots, scenario, repositoryRoot) {
  const { default: assert } = await import("node:assert/strict");
  const { existsSync, mkdirSync, readFileSync, readlinkSync, statSync, writeFileSync } = await import("node:fs");
  const { default: path } = await import("node:path");
  const { spawnSync } = await import("node:child_process");
  const { extract } = await import("tar");
  const { createContext, SourceTextModule, SyntheticModule } = await import("node:vm");
  const projectRoot = process.cwd();
  for (const [relative, content] of [
    ["package.json", '{"version":"fixture"}\n'], ["bun.lock", "bun\n"],
    ["uv.lock", "uv\n"], ["pyproject.toml", "[project]\n"],
    ["app/source.py", "source\n"], ["configs/input.jsonc", "{}\n"],
    ["resources/input.txt", "resource\n"], ["src/shared/sse.js", "export {};\n"],
    ["src/clients/web/index.ts", "export {};\n"],
    ["src/clients/workbench/src/main.ts", "export {};\n"],
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
  mkdirSync(path.join(projectRoot, "scripts/plugins"), { recursive: true });
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
  const stopAfterExtract = new Error("fixture在源码复制前结束");
  const runtimePackageRoot = path.join(projectRoot, "out/packaging", target, "stage", `runtime-${target}`, "package");
  const executable = mode === "linux" ? "python/bin/python3" : "python/python.exe";
  const archivePath = path.join(projectRoot, "Python 中文 %&.tar.gz");
  const nodeArchivePath = path.join(projectRoot, "Node 中文.tar.gz");
  const artifactsRoot = path.dirname(process.argv[2]);
  // 独立标准库生成归档，避免同一个库的写入和读取互相掩盖问题。
  const pythonArgs = ["run", "--no-sync", "--project", repositoryRoot, "python", "-c", `
import io, sys, tarfile
archive, executable, scenario, links, node_archive = sys.argv[1:]
with tarfile.open(archive, 'w:gz') as output:
    entries = [('python/中文 空格 %&.txt', b'fixture-data', 0o644)]
    if scenario != 'missing-python':
        entries.append((executable, b'fixture-python', 0o755))
    for name, body, mode in entries:
        entry = tarfile.TarInfo(name)
        entry.size, entry.mode = len(body), mode
        output.addfile(entry, io.BytesIO(body))
    if links == 'yes':
        entry = tarfile.TarInfo('python/current')
        entry.type, entry.linkname = tarfile.SYMTYPE, 'bin/python3'
        output.addfile(entry)
with tarfile.open(node_archive, 'w:gz') as output:
    for name in ['node-v22.17.0-linux-x64/bin/node', 'node-v22.17.0-linux-x64/LICENSE']:
        entry = tarfile.TarInfo(name)
        body = b'fixture-node'
        entry.size, entry.mode = len(body), 0o755
        output.addfile(entry, io.BytesIO(body))
`, archivePath, executable, scenario, mode === "linux" && process.platform !== "win32" ? "yes" : "no", nodeArchivePath];
  const python = spawnSync("uv", pythonArgs, { timeout: 5000 });
  writeFileSync(path.join(artifactsRoot, "archive-fixture.stdout.raw"), python.stdout ?? "");
  writeFileSync(path.join(artifactsRoot, "archive-fixture.stderr.raw"), python.stderr ?? "");
  writeFileSync(path.join(artifactsRoot, "archive-fixture.result.json"), JSON.stringify({
    command: ["uv", ...pythonArgs], status: python.status, signal: python.signal, error: python.error?.message ?? null,
  }));
  assert.equal(python.status, 0, String(python.stderr));
  if (scenario === "corrupt") writeFileSync(archivePath, "broken gzip archive");
  let copied = false;
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
  // 原构建入口全文执行；下载返回本地归档，解包使用真实库，源码复制前停止。
  const module = new SourceTextModule(readFileSync(new URL(scriptUrl), "utf8"), { identifier: scriptUrl, context });
  await module.link(async (specifier) => {
    let exports;
    if (specifier === "./pinned-archive-download.mjs") {
      exports = Object.fromEntries([
        ["downloadPinnedLinuxPython", "linux-python"],
        ["downloadPinnedLinuxNode", "linux-node"],
        ["downloadPinnedWindowsPython", "windows-python"],
        ["downloadPinnedWindowsNode", "windows-node"],
      ].map(([name, kind]) => [name, async (root) => {
        assert.equal(existsSync(buildResultPath), false, "下载开始前旧成功metadata必须已删除");
        calls.push([kind, root]);
        return kind === "linux-node" ? nodeArchivePath : kind === "windows-node" ? path.join(root, `${kind}.archive`) : archivePath;
      }]));
    } else if (specifier === "tar") {
      exports = { async extract(options, entries) {
        calls.push(["extract", { ...options }, ...(entries ? [Array.from(entries)] : [])]);
        await extract(options, entries);
      } };
    } else if (specifier === "./application-sources.mjs") {
      exports = { copyApplicationSources() { copied = true; throw stopAfterExtract; } };
    } else if (specifier === "../../scripts/plugins/pack-developer-tools.mjs") {
      exports = { async packDeveloperTools() { throw new Error("归档检查应在源码复制前结束"); } };
    } else if (specifier === "../../scripts/plugins/pack-mcp-stdio.mjs") {
      exports = { async packMcpStdio() { throw new Error("归档检查应在源码复制前结束"); } };
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
      if (specifier === "node:child_process" || specifier === "cross-spawn") {
        const spawnSync = (command, args, options) => {
          throw new Error(`解包检查不应启动外部命令: ${command} ${args} cwd=${options.cwd}`);
        };
        if (specifier === "cross-spawn") exports = { default: { sync: spawnSync } };
        else exports.spawnSync = spawnSync;
      }
    }
    return new SyntheticModule(Object.keys(exports), function () {
      for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
    }, { context });
  });
  if (mode === "windows-cross") {
    await assert.rejects(module.evaluate(), (error) => {
      assert.match(error.message, /windows-x64 构建器不支持当前模式: host=linux-x64 cross=true/);
      return true;
    });
    assert.deepEqual(calls, [], "Linux --cross 必须在构建快照、下载与解包前拒绝");
    assert.equal(readFileSync(buildResultPath, "utf8"), "stale success\n", "拒绝模式不能改写旧构建metadata");
    for (const directory of [
      ...["stage", "tarballs", "release-assets", "standalone", "installer"]
        .map((name) => path.join(projectRoot, "out/packaging", target, name)),
      defaultDownloadRoot,
      pythonDownloadRoot,
      nodeDownloadRoot,
    ]) {
      assert.equal(existsSync(directory), false, `拒绝模式不得创建 ${directory}`);
    }
    return { downloads: 0, rejectedBeforeWrites: true, extraction: scenario };
  }
  process.env.PATH = "";
  await assert.rejects(module.evaluate(), (error) => {
    if (scenario === "corrupt") return error.code === "TAR_BAD_ARCHIVE";
    if (scenario === "missing-python") return error.message.includes("解压后缺少") && error.message.includes("Python");
    return error === stopAfterExtract;
  });
  assert.equal(copied, scenario === "valid", "解包失败或缺少Python时不能进入源码复制");
  if (scenario !== "corrupt") {
    assert.equal(readFileSync(path.join(runtimePackageRoot, "python/中文 空格 %&.txt"), "utf8"), "fixture-data");
  }
  if (scenario === "valid") {
    assert.equal(readFileSync(path.join(runtimePackageRoot, executable), "utf8"), "fixture-python");
    if (mode === "linux" && process.platform !== "win32") {
      // 继承ACL可能像原系统tar一样将0755收窄为0750，必须保留所有者执行位且不能扩大权限。
      const permissions = statSync(path.join(runtimePackageRoot, executable)).mode & 0o777;
      assert.equal(permissions & 0o700, 0o700);
      assert.equal(permissions & ~0o755, 0);
      assert.equal(readlinkSync(path.join(runtimePackageRoot, "python/current")), "bin/python3");
      assert.equal(readFileSync(path.join(runtimePackageRoot, "python/current"), "utf8"), "fixture-python");
    }
  }
  assert.equal(calls[0][0], "source-snapshot");
  assert.equal(calls[0][1], initialFingerprint);
  assert.equal(existsSync(buildResultPath), false, "失败构建不能复用旧成功metadata");
  const expectedPythonRoot = mode === "linux" ? defaultDownloadRoot : pythonDownloadRoot;
  const pythonKind = mode === "linux" ? "linux-python" : "windows-python";
  const expected = [[pythonKind, expectedPythonRoot]];
  if (mode !== "linux") expected.push(["windows-node", nodeDownloadRoot]);
  expected.push(["extract", { file: archivePath, cwd: runtimePackageRoot, strict: true }]);
  if (mode === "linux" && scenario === "valid") {
    expected.push(["linux-node", defaultDownloadRoot]);
    expected.push(["extract", { file: nodeArchivePath, cwd: path.join(runtimePackageRoot, "node"), strip: 1, strict: true }, ["node-v22.17.0-linux-x64/bin/node", "node-v22.17.0-linux-x64/LICENSE"]]);
  }
  // VM数组属于另一realm，序列化后核对实际下载顺序、目录和首次解包参数。
  assert.deepEqual(JSON.parse(JSON.stringify(calls.slice(1))), expected);
  return { downloads: expected.filter(([kind]) => kind !== "extract").length, snapshotMatchesInitial: true, oldMetadataRemoved: true, extraction: scenario };
}

describe("构建入口将下载目录交给唯一归档owner", () => {
  const cases = ["linux", "windows-native"].flatMap((mode) => [
    [mode, false, "valid"], [mode, true, "valid"],
    [mode, false, "corrupt"], [mode, false, "missing-python"],
  ]).concat(["windows-cross"].flatMap((mode) => [
    [mode, false, "valid"], [mode, true, "valid"],
  ]));
  for (const [mode, overrideRoots, extraction] of cases) {
    test(`${mode}/${overrideRoots ? "独立下载根" : "默认下载根"}/${extraction}`, () => {
      const scenario = `${overrideRoots ? "override-download-roots" : "default-download-root"}-${extraction}`;
      const fixtureRoot = path.join(fixtureRunRoot, mode, scenario);
      const artifactsRoot = path.join(artifactsRunRoot, mode, scenario);
      for (const directory of [fixtureRoot, artifactsRoot]) {
        mkdirSync(path.dirname(directory), { recursive: true });
        mkdirSync(directory);
      }
      const scriptUrl = new URL(mode === "linux" ? "./build-linux-x64.mjs" : "./build-windows-x64.mjs", import.meta.url).href;
      const inputPath = path.join(artifactsRoot, "input.json");
      writeFileSync(inputPath, `${JSON.stringify([scriptUrl, mode, overrideRoots, extraction, process.cwd()])}\n`, { flag: "wx" });
      const args = ["--experimental-vm-modules", observerPath, inputPath];
      const run = spawnSync("node", args, { cwd: fixtureRoot, timeout: 15000 });
      const stdout = run.stdout ?? Buffer.alloc(0);
      const stderr = run.stderr ?? Buffer.alloc(0);
      writeFileSync(path.join(artifactsRoot, "stdout.raw"), stdout, { flag: "wx" });
      writeFileSync(path.join(artifactsRoot, "stderr.raw"), stderr, { flag: "wx" });
      writeFileSync(path.join(artifactsRoot, "result.json"), `${JSON.stringify({
        command: ["node", ...args], cwd: fixtureRoot, timeout_ms: 15000,
        status: run.status, signal: run.signal,
        error: run.error ? { name: run.error.name, message: run.error.message, code: run.error.code ?? null } : null,
      }, null, 2)}\n`, { flag: "wx" });
      if (run.error || run.status !== 0) {
        throw new Error(`构建下载接线 ${mode}/${scenario} 失败: exit=${run.status} signal=${run.signal}；artifacts=${artifactsRoot}\n${stderr.toString("utf8")}`, { cause: run.error });
      }
      expect(JSON.parse(stdout.toString("utf8"))).toEqual(mode === "windows-cross"
        ? { downloads: 0, rejectedBeforeWrites: true, extraction }
        : { downloads: mode === "linux" && extraction !== "valid" ? 1 : 2,
          snapshotMatchesInitial: true, oldMetadataRemoved: true, extraction });
    });
  }
});
