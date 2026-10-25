import { describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const outputRoot = path.resolve("out/tests/scripts/install/install-release-cache");
const runId = randomUUID();
const workspaceRunRoot = path.join(outputRoot, "workspace", runId);
const artifactsRunRoot = path.join(outputRoot, "artifacts", runId);
const observerPath = path.join(artifactsRunRoot, "observer.mjs");

function serializeError(error) {
  return error === undefined || error === null
    ? null
    : {
        name: error.name,
        message: error.message,
        code: error.code ?? null,
        errno: error.errno ?? null,
        syscall: error.syscall ?? null,
      };
}

function writeExclusive(filePath, contents) {
  writeFileSync(filePath, contents, { flag: "wx" });
}

function observerSource() {
  return `${observeLocalRelease.toString()}\n` +
    'const [scriptUrl, targetPlatform, scenario] = JSON.parse(process.argv[2]);\n' +
    'const result = await observeLocalRelease(scriptUrl, targetPlatform, scenario);\n' +
    'console.log(JSON.stringify(result));\n';
}

mkdirSync(artifactsRunRoot, { recursive: true });
writeExclusive(observerPath, observerSource());

function resultArtifact({ input, result, stdout, stderr }) {
  return {
    ...input,
    actual_exit_code: result.status,
    signal: result.signal,
    spawn_error: serializeError(result.error),
    stdout_bytes: Buffer.byteLength(stdout),
    stderr_bytes: Buffer.byteLength(stderr),
    unfinished_stage:
      result.error || result.status !== 0 ? "isolated-node-scenario" : null,
  };
}

function saveProcessArtifacts({ artifactRoot, input, result }) {
  const stdout = result.stdout ?? "";
  const stderr = result.stderr ?? "";
  writeExclusive(path.join(artifactRoot, "input.json"), `${JSON.stringify(input, null, 2)}\n`);
  writeExclusive(path.join(artifactRoot, "stdout.raw"), stdout);
  writeExclusive(path.join(artifactRoot, "stderr.raw"), stderr);
  writeExclusive(
    path.join(artifactRoot, "result.json"),
    `${JSON.stringify(resultArtifact({ input, result, stdout, stderr }), null, 2)}\n`,
  );
  return { stdout, stderr };
}

async function observeLocalRelease(scriptUrl, targetPlatform, scenario) {
  const { default: assert } = await import("node:assert/strict");
  const { default: fs } = await import("node:fs");
  const { default: path } = await import("node:path");
  const { createHash } = await import("node:crypto");
  const { syncBuiltinESMExports } = await import("node:module");
  const projectRoot = process.cwd();
  const write = (relative, body) => {
    const file = path.join(projectRoot, relative);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, body);
  };
  // 最小完整构建输入；由真实指纹owner读取，不注入伪造的源码指纹。
  const sourceFiles = [
    ["package.json", '{"version":"1.2.3"}\n'], ["bun.lock", "bun"], ["uv.lock", "uv"],
    ["pyproject.toml", "python"], ["app/example.py", "before\n"], ["configs/example.jsonc", "{}"],
    ["resources/example.txt", "resource"], ["src/clients/web/index.ts", "web"],
    ["src/shared/sseRuntimeValidators.js", "shared before\n"],
    ["src/workspace-services/example.ts", "service"], ["packages/launcher/package.json", "{}"],
    ["packaging/runtime/example.mjs", "runtime"], ["scripts/install/runtime-postinstall.mjs", "postinstall"],
    [`packages/runtime-${targetPlatform}/package.json`, "{}"],
    [`scripts/release/package-${targetPlatform}.mjs`, "build"],
  ];
  const sourceDirectories = new Set(
    sourceFiles
      .map(([relative]) => path.dirname(relative))
      .filter((directory) => directory !== "."),
  );
  for (const directory of sourceDirectories) {
    fs.mkdirSync(path.join(projectRoot, directory), { recursive: true });
  }
  for (const [relative, body] of sourceFiles) {
    fs.writeFileSync(path.join(projectRoot, relative), body);
  }
  process.env.BOXTEAM_PROJECT_ROOT = projectRoot;
  const { buildReleaseInstallCommand, ensureCurrentLocalPackage } = await import(scriptUrl);
  const { computeBuildInputFingerprint } = await import(new URL("../../packaging/runtime/build-input-fingerprint.mjs", scriptUrl));
  const command = buildReleaseInstallCommand({ projectRoot, version: "1.2.3", architecture: "x64",
    platform: targetPlatform === "linux-x64" ? "linux" : "win32" });
  // 跨过默认文件流缓冲区 1 字节，确保摘要观察覆盖尾部短块。
  const payloads = [
    Buffer.from("launcher archive"),
    Buffer.alloc(64 * 1024 + 1, 0x52),
  ];
  command.packageTarballs.forEach((file, index) => write(path.relative(projectRoot, file), payloads[index]));
  const digests = payloads.map((body) => createHash("sha256").update(body).digest("hex"));
  const metadata = {
    platform: targetPlatform, version: command.version,
    source_fingerprint: computeBuildInputFingerprint({ projectRoot, targetPlatform }),
    install_tarballs: {
      launcher: { filename: path.basename(command.packageTarballs[0]), sha256: digests[0] },
      runtime: { filename: path.basename(command.packageTarballs[1]), sha256: digests[1] },
    },
  };
  const stale = structuredClone(metadata);
  if (scenario === "stale-platform") stale.platform = targetPlatform === "linux-x64" ? "windows-x64" : "linux-x64";
  if (scenario === "stale-version") stale.version = "0.0.0";
  if (["stale-source", "failed-build", "invalid-rebuild", "source-changed-during-build"].includes(scenario)) stale.source_fingerprint = "0".repeat(64);
  if (scenario === "bad-launcher-digest") stale.install_tarballs.launcher.sha256 = "0".repeat(64);
  if (scenario === "bad-runtime-digest") stale.install_tarballs.runtime.sha256 = "0".repeat(64);
  write(path.relative(projectRoot, command.buildResult), JSON.stringify(stale));
  if (scenario === "stale-shared-source") write("src/shared/sseRuntimeValidators.js", "shared changed\n");
  const archiveReadAttempts = [];
  const readError = new Error("injected matching archive read failure");
  const originalReadStream = fs.createReadStream;
  let phase = "initial";
  let buildAttemptCount = 0;
  let operationError;
  fs.createReadStream = (file, ...args) => {
    const index = command.packageTarballs.indexOf(String(file));
    if (index < 0) return originalReadStream(file, ...args);
    if (scenario === "hash-read-error") throw readError;
    const archiveReadAttempt = { phase, archiveIndex: index, bytes: 0 };
    archiveReadAttempts.push(archiveReadAttempt);
    const stream = originalReadStream(file, ...args);
    stream.on("data", (chunk) => {
      archiveReadAttempt.bytes += chunk.length;
    });
    return stream;
  };
  syncBuiltinESMExports();
  try {
    try {
      await ensureCurrentLocalPackage({ command, projectRoot, environment: { FIXTURE_BUILD: "1" },
        spawnSyncImpl(executable, args, options) {
          buildAttemptCount += 1;
          assert.equal(executable, process.execPath);
          assert.deepEqual(args, [command.buildScript]);
          assert.equal(options.cwd, projectRoot);
          assert.deepEqual(options.env, { FIXTURE_BUILD: "1", BOXTEAM_PROJECT_ROOT: projectRoot });
          if (scenario === "failed-build") return { status: 17 };
          phase = "rebuilt";
          const rebuilt = structuredClone(metadata);
          if (scenario === "stale-shared-source") rebuilt.source_fingerprint = computeBuildInputFingerprint({ projectRoot, targetPlatform });
          if (scenario === "invalid-rebuild") rebuilt.version = "0.0.0";
          if (scenario === "source-changed-during-build") write("app/example.py", "changed during build\n");
          write(path.relative(projectRoot, command.buildResult), JSON.stringify(rebuilt));
          return { status: 0 };
        },
      });
    } catch (error) {
      operationError = error;
    }
    const metadataRejectsCacheReuse = [
      "stale-platform",
      "stale-version",
      "stale-source",
      "stale-shared-source",
      "failed-build",
      "invalid-rebuild",
      "source-changed-during-build",
    ].includes(scenario);
    const initialArchiveReadAttempts = archiveReadAttempts.filter(
      (attempt) => attempt.phase === "initial",
    );
    const rebuiltArchiveReadAttempts = archiveReadAttempts.filter(
      (attempt) => attempt.phase === "rebuilt",
    );
    if (scenario === "hash-read-error") {
      assert.equal(operationError, readError);
      assert.equal(buildAttemptCount, 0, "当前缓存的IO错误不能被当成过期而静默重建");
    } else {
      assert.equal(
        initialArchiveReadAttempts.length,
        metadataRejectsCacheReuse ? 0 : 2,
        "轻量元数据已拒绝的归档不能再读取正文",
      );
      assert.equal(buildAttemptCount, scenario === "valid-cache" ? 0 : 1);
      if (scenario === "failed-build") {
        assert.match(operationError?.message ?? "", /构建失败:.*exit=17/);
      } else if (["invalid-rebuild", "source-changed-during-build"].includes(scenario)) {
        assert.match(operationError?.message ?? "", /构建结果与当前源码不一致/);
      } else {
        assert.equal(operationError, undefined);
      }
      const shouldHashRebuiltArchives = [
        "stale-platform",
        "stale-version",
        "stale-source",
        "stale-shared-source",
        "bad-launcher-digest",
        "bad-runtime-digest",
      ].includes(scenario);
      assert.equal(
        rebuiltArchiveReadAttempts.length,
        shouldHashRebuiltArchives ? 2 : 0,
      );
    }
    for (const attempt of archiveReadAttempts) {
      assert.equal(
        attempt.bytes,
        payloads[attempt.archiveIndex].length,
        "接受缓存或重建结果前仍须读取完整归档校验摘要",
      );
    }
    return {
      targetPlatform,
      scenario,
      fixtureSourceFingerprint: metadata.source_fingerprint,
      cacheMetadataSourceFingerprint: stale.source_fingerprint,
      finalPhase: phase,
      buildAttemptCount,
      initialArchiveReadAttempts: initialArchiveReadAttempts.length,
      rebuiltArchiveReadAttempts: rebuiltArchiveReadAttempts.length,
      operationError: operationError
        ? `${operationError.name}: ${operationError.message}`
        : null,
    };
  } finally {
    fs.createReadStream = originalReadStream;
    syncBuiltinESMExports();
  }
}

describe("真实本地发布入口的缓存校验顺序", () => {
  for (const targetPlatform of ["linux-x64", "windows-x64"]) {
    for (const scenario of ["valid-cache", "stale-platform", "stale-version", "stale-source", "stale-shared-source", "bad-launcher-digest",
      "bad-runtime-digest", "failed-build", "invalid-rebuild", "source-changed-during-build", "hash-read-error"]) {
      test(`${targetPlatform}/${scenario}先核归属及版本，再读取需要复用的归档`, () => {
        const workspace = path.join(
          workspaceRunRoot,
          targetPlatform,
          `${scenario}-源码 fixture`,
        );
        const artifactRoot = path.join(artifactsRunRoot, targetPlatform, scenario);
        mkdirSync(workspace, { recursive: true });
        mkdirSync(artifactRoot, { recursive: true });
        const args = [
          observerPath,
          JSON.stringify([
            new URL("./install-release.mjs", import.meta.url).href,
            targetPlatform,
            scenario,
          ]),
        ];
        const input = {
          command: ["node", ...args],
          cwd: workspace,
          timeout_ms: 5000,
          target_platform: targetPlatform,
          scenario,
          stage: "isolated-node-scenario",
        };
        const run = spawnSync("node", args, {
          cwd: workspace,
          encoding: "utf8",
          timeout: input.timeout_ms,
        });
        const { stdout, stderr } = saveProcessArtifacts({
          artifactRoot,
          input,
          result: run,
        });
        if (run.error || run.status !== 0) {
          const detail = stderr || `本地缓存探针退出: ${run.status}`;
          throw new Error(
            `${targetPlatform}/${scenario} workspace=${workspace} artifacts=${artifactRoot} ` +
              `exit=${String(run.status)}\n${detail}`,
            { cause: run.error },
          );
        }
        expect(JSON.parse(stdout.trim().split("\n").at(-1)).scenario).toBe(scenario);
      });
    }
  }
});
