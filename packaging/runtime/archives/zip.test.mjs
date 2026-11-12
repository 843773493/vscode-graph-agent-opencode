import { expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  chmodSync, lstatSync, mkdirSync, readFileSync, symlinkSync, writeFileSync,
} from "node:fs";
import path from "node:path";
import * as fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";

import { createZipArchive, extractZipArchive } from "./zip.mjs";
import * as versions from "../versions.mjs";

const outputRoot = path.resolve("out/tests/packaging/runtime/archives/zip");
const runId = randomUUID();

function scenario(name) {
  const directories = Object.fromEntries(["workspace", "artifacts"].map((kind) => {
    const directory = path.join(outputRoot, kind, runId, name);
    mkdirSync(directory, { recursive: true });
    return [kind, directory];
  }));
  return directories;
}

function python(artifacts, script, args) {
  const command = ["run", "--no-sync", "python", "-c", script, ...args];
  const result = spawnSync("uv", command, { encoding: "utf8", timeout: 15_000 });
  writeFileSync(path.join(artifacts, "stdout.raw"), result.stdout ?? "");
  writeFileSync(path.join(artifacts, "stderr.raw"), result.stderr ?? "");
  writeFileSync(path.join(artifacts, "result.json"), JSON.stringify({
    command: ["uv", ...command], cwd: process.cwd(), status: result.status,
    signal: result.signal, error: result.error?.message ?? null,
  }, null, 2));
  if (result.error) throw result.error;
  expect(result.status).toBe(0);
  return result.stdout.trim();
}

test("流式 ZIP 保留便携版顶层、空目录、二进制内容、权限和链接目标", async () => {
  const { workspace, artifacts } = scenario("roundtrip");
  const source = path.join(workspace, "boxteam 便携版 & %");
  mkdirSync(path.join(source, "中文 目录", "空目录"), { recursive: true });
  const binary = Buffer.alloc(2 * 1024 * 1024);
  for (let index = 0; index < binary.length; index++) binary[index] = index % 251;
  writeFileSync(path.join(source, "node.exe"), binary);
  writeFileSync(path.join(source, "中文 目录", "文本.txt"), "中文内容\n");
  writeFileSync(path.join(source, "empty.txt"), "");
  chmodSync(path.join(source, "node.exe"), 0o755);
  if (process.platform !== "win32") {
    symlinkSync("node.exe", path.join(source, "linked.exe"));
    symlinkSync("中文 目录", path.join(source, "linked-dir"));
  }
  const archive = path.join(workspace, "release.zip");
  await createZipArchive(source, archive);
  // 用独立标准库核对 ZIP 格式及正文，避免写入库和解压库相互掩盖问题。
  const entries = JSON.parse(python(artifacts, `
import json,sys,zipfile
with zipfile.ZipFile(sys.argv[1]) as archive:
    assert archive.testzip() is None
    print(json.dumps({entry.filename: entry.external_attr >> 16 for entry in archive.infolist()}, ensure_ascii=False))
`, [archive]));
  const top = path.basename(source);
  expect(Object.keys(entries).sort()).toEqual([
    `${top}/`, `${top}/node.exe`, `${top}/empty.txt`, `${top}/中文 目录/`,
    `${top}/中文 目录/文本.txt`, `${top}/中文 目录/空目录/`,
    ...(process.platform === "win32" ? [] : [
      `${top}/linked.exe`, `${top}/linked-dir/`, `${top}/linked-dir/文本.txt`, `${top}/linked-dir/空目录/`,
    ]),
  ].sort());
  if (process.platform !== "win32") expect(entries[`${top}/node.exe`] & 0o777).toBe(0o755);
  const extracted = path.join(workspace, "解压 输出");
  await extractZipArchive(archive, { dir: extracted });
  const root = path.join(extracted, top);
  expect(readFileSync(path.join(root, "node.exe"))).toEqual(binary);
  expect(readFileSync(path.join(root, "中文 目录", "文本.txt"), "utf8")).toBe("中文内容\n");
  expect(lstatSync(path.join(root, "中文 目录", "空目录")).isDirectory()).toBe(true);
  if (process.platform !== "win32") {
    expect(lstatSync(path.join(root, "linked.exe")).isSymbolicLink()).toBe(false);
    expect(readFileSync(path.join(root, "linked.exe"))).toEqual(binary);
    expect(readFileSync(path.join(root, "linked-dir", "文本.txt"), "utf8")).toBe("中文内容\n");
  }
});

test("解压独立生成的含 ZIP64 文件头的 Node ZIP", async () => {
  const { workspace, artifacts } = scenario("independent-zip64");
  const archive = path.join(workspace, "node archive.zip");
  python(artifacts, `
import sys,zipfile
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as archive:
    with archive.open("node-version/node.exe", "w", force_zip64=True) as stream:
        stream.write(bytes(range(256)))
    archive.writestr("node-version/中文 空格 & %.txt", "内容")
`, [archive]);
  const extracted = path.join(workspace, "extracted");
  await extractZipArchive(archive, { dir: extracted });
  expect(readFileSync(path.join(extracted, "node-version/node.exe"))).toEqual(Buffer.from(Array.from({ length: 256 }, (_, index) => index)));
  expect(readFileSync(path.join(extracted, "node-version/中文 空格 & %.txt"), "utf8")).toBe("内容");
});

test("缺失输入、不可写输出和损坏 ZIP 均明确失败", async () => {
  const { workspace } = scenario("failures");
  await expect(createZipArchive(path.join(workspace, "missing"), path.join(workspace, "missing.zip"))).rejects.toThrow();
  const source = path.join(workspace, "source");
  mkdirSync(source);
  writeFileSync(path.join(source, "file.txt"), "content");
  await expect(createZipArchive(source, workspace)).rejects.toThrow();
  const archive = path.join(workspace, "broken.zip");
  writeFileSync(archive, "not a zip");
  await expect(extractZipArchive(archive, { dir: path.join(workspace, "extracted") })).rejects.toThrow();
});

test("便携版 staging 复制完整 runtime 并等待最终 ZIP 写完", async () => {
  const { workspace } = scenario("standalone-staging");
  const runtimePackageRoot = path.join(workspace, "runtime-package");
  const launcherPackageRoot = path.join(workspace, "launcher-package");
  const nodeDirectory = path.join(runtimePackageRoot, "node");
  mkdirSync(nodeDirectory, { recursive: true });
  mkdirSync(launcherPackageRoot);
  writeFileSync(path.join(nodeDirectory, "node.exe"), "fixture-node-bytes");
  writeFileSync(path.join(nodeDirectory, "LICENSE"), "fixture-node-license");
  writeFileSync(path.join(runtimePackageRoot, "runtime-manifest.json"), JSON.stringify({ chromium_executable: "chromium/chrome.exe" }));
  writeFileSync(path.join(launcherPackageRoot, "package.json"), JSON.stringify({
    name: "boxteam", dependencies: { open: "11.0.4" }, optionalDependencies: { "@boxteam/runtime-windows-x64": "fixture" },
  }));
  const executable = path.join(workspace, "BoxTeam.exe");
  writeFileSync(executable, "fixture-native-launcher");
  const parsed = ts.createSourceFile("build-windows-x64.mjs",
    readFileSync(new URL("../build-windows-x64.mjs", import.meta.url), "utf8"),
    ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  const names = ["stageStandalonePackage", "writeRuntimeMetadata", "writeStandaloneLauncherScripts"];
  const declarations = parsed.statements.filter(node => ts.isFunctionDeclaration(node) && names.includes(node.name?.text));
  expect(declarations).toHaveLength(names.length);
  const commands = [];
  const packageStageRoot = path.join(workspace, "short-stage");
  const context = vm.createContext({
    ...fs, ...versions, path, createZipArchive, extractZipArchive,
    projectRoot: workspace,
    runtimePackageRoot, launcherPackageRoot,
    standaloneStageRoot: path.join(workspace, "stage"), standaloneRoot: path.join(workspace, "releases"),
    standaloneDirectoryName: "boxteam-fixture", buildWindowsLauncher: () => executable,
    mkdtempSync() { mkdirSync(packageStageRoot); return packageStageRoot; },
    run(command, args) { commands.push([command, Array.from(args)]); },
  });
  vm.runInContext(declarations.map(node => node.getText(parsed)).join("\n"), context);
  const staged = await context.stageStandalonePackage();
  const archive = staged.standaloneArchive;
  expect(archive).toBe(path.join(workspace, "releases/boxteam-fixture.zip"));
  expect(staged.packageStageRoot).toBe(packageStageRoot);
  expect(commands).toEqual([["bun", ["install", "--production", "--ignore-scripts", "--os", "win32", "--cpu", "x64"]]]);
  const extracted = path.join(workspace, "extracted");
  await extractZipArchive(archive, { dir: extracted });
  const root = path.join(extracted, "boxteam-fixture");
  expect(readFileSync(path.join(root, "runtime/node/node.exe"), "utf8")).toBe("fixture-node-bytes");
  expect(readFileSync(path.join(root, "runtime/node/LICENSE"), "utf8")).toBe("fixture-node-license");
  expect(readFileSync(path.join(root, "BoxTeam.exe"), "utf8")).toBe("fixture-native-launcher");
  expect(readFileSync(path.join(root, "BoxTeamDoctor.exe"), "utf8")).toBe("fixture-native-launcher");
  const manifest = JSON.parse(readFileSync(path.join(root, "runtime/runtime-manifest.json"), "utf8"));
  expect(manifest.distribution).toBe("standalone");
  expect(manifest.node).toEqual({ source: "bundled", executable: "node/node.exe" });
  const launcher = JSON.parse(readFileSync(path.join(root, "launcher/package.json"), "utf8"));
  expect(launcher.dependencies).toEqual({ open: "11.0.4" });
  expect(launcher.optionalDependencies).toBeUndefined();
});
