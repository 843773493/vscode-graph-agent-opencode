import { describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync } from "node:fs";
import path from "node:path";

const fixtureRunRoot = path.resolve(
  "out/tests/scripts/install/install-release-cache/workspace", randomUUID(),
);

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
  for (const [relative, body] of [
    ["package.json", '{"version":"1.2.3"}\n'], ["bun.lock", "bun"], ["uv.lock", "uv"],
    ["pyproject.toml", "python"], ["app/example.py", "before\n"], ["configs/example.jsonc", "{}"],
    ["resources/example.txt", "resource"], ["src/clients/web/index.ts", "web"],
    ["src/shared/sseRuntimeValidators.js", "shared before\n"],
    ["src/workspace-services/example.ts", "service"], ["packages/launcher/package.json", "{}"],
    ["packaging/runtime/example.mjs", "runtime"], ["scripts/install/runtime-postinstall.mjs", "postinstall"],
    [`packages/runtime-${targetPlatform}/package.json`, "{}"],
    [`scripts/release/package-${targetPlatform}.mjs`, "build"],
  ]) write(relative, body);
  process.env.BOXTEAM_PROJECT_ROOT = projectRoot;
  const { buildReleaseInstallCommand, ensureCurrentLocalPackage } = await import(scriptUrl);
  const { computeBuildInputFingerprint } = await import(new URL("../../packaging/runtime/build-input-fingerprint.mjs", scriptUrl));
  const command = buildReleaseInstallCommand({ projectRoot, version: "1.2.3", architecture: "x64",
    platform: targetPlatform === "linux-x64" ? "linux" : "win32" });
  const payloads = [Buffer.from("launcher archive"), Buffer.from("runtime archive\n".repeat(8192))];
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
  const reads = [], readError = new Error("injected matching archive read failure");
  const originalReadStream = fs.createReadStream;
  let phase = "initial", builds = 0, failure;
  fs.createReadStream = (file, ...args) => {
    const index = command.packageTarballs.indexOf(String(file));
    if (index < 0) return originalReadStream(file, ...args);
    if (scenario === "hash-read-error") throw readError;
    const observation = { phase, archive: index, bytes: 0 };
    reads.push(observation);
    const stream = originalReadStream(file, ...args);
    stream.on("data", (chunk) => { observation.bytes += chunk.length; });
    return stream;
  };
  syncBuiltinESMExports();
  try {
    try {
      await ensureCurrentLocalPackage({ command, projectRoot, environment: { FIXTURE_BUILD: "1" },
        spawnSyncImpl(executable, args, options) {
          builds++;
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
    } catch (error) { failure = error; }
    const metadataRejects = ["stale-platform", "stale-version", "stale-source", "stale-shared-source", "failed-build",
      "invalid-rebuild", "source-changed-during-build"].includes(scenario);
    const initialReads = reads.filter((read) => read.phase === "initial");
    const rebuiltReads = reads.filter((read) => read.phase === "rebuilt");
    if (scenario === "hash-read-error") {
      assert.equal(failure, readError);
      assert.equal(builds, 0, "当前缓存的IO错误不能被当成过期而静默重建");
    } else {
      assert.equal(initialReads.length, metadataRejects ? 0 : 2, "轻量元数据已拒绝的归档不能再读取正文");
      assert.equal(builds, scenario === "valid-cache" ? 0 : 1);
      if (scenario === "failed-build") assert.match(failure?.message ?? "", /构建失败:.*exit=17/);
      else if (["invalid-rebuild", "source-changed-during-build"].includes(scenario)) assert.match(failure?.message ?? "", /构建结果与当前源码不一致/);
      else assert.equal(failure, undefined);
      const rejectsRebuilt = ["failed-build", "invalid-rebuild", "source-changed-during-build", "valid-cache"].includes(scenario);
      assert.equal(rebuiltReads.length, rejectsRebuilt ? 0 : 2);
    }
    for (const read of reads) assert.equal(read.bytes, payloads[read.archive].length, "接受缓存或重建结果前仍须读取完整归档校验摘要");
    return { targetPlatform, scenario, builds, initialReads: initialReads.length, rebuiltReads: rebuiltReads.length };
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
        const fixtureRoot = path.join(fixtureRunRoot, targetPlatform, `${scenario}-源码 fixture`);
        mkdirSync(fixtureRoot, { recursive: true });
        const run = spawnSync("node", ["--input-type=module", "--eval",
          `${observeLocalRelease.toString()}\nconsole.log(JSON.stringify(await observeLocalRelease(...JSON.parse(process.argv[1]))));`,
          JSON.stringify([new URL("./install-release.mjs", import.meta.url).href, targetPlatform, scenario]),
        ], { cwd: fixtureRoot, encoding: "utf8", timeout: 5000 });
        if (run.error || run.status !== 0) {
          const detail = run.stderr || `本地缓存探针退出: ${run.status}`;
          throw new Error(
            `${targetPlatform}/${scenario} fixture=${fixtureRoot} exit=${String(run.status)}\n${detail}`,
            { cause: run.error },
          );
        }
        expect(JSON.parse(run.stdout.trim().split("\n").at(-1)).scenario).toBe(scenario);
      });
    }
  }
});
