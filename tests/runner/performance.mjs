import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createWriteStream } from "node:fs";
import { once } from "node:events";
import { finished } from "node:stream/promises";
import path from "node:path";
import os from "node:os";

export const performanceModes = ["off", "basic", "core", "diagnostic"];

export function collectionOptions(suite, overrides = {}) {
  const collection = { mode: "basic", sample_interval_ms: 250, sample_limit_bytes: 64 * 1024 * 1024,
    ...suite.performance?.collection, ...overrides };
  if (!performanceModes.includes(collection.mode)) throw new Error("未知 performance mode");
  if (collection.tool !== undefined && (!['cpu', 'allocations', 'browser-cpu', 'browser-trace'].includes(collection.tool) || collection.mode !== 'diagnostic')) {
    throw new Error("--perf-tool 需要 diagnostic 档位，支持 cpu/allocations/browser-cpu/browser-trace");
  }
  for (const key of ["sample_interval_ms", "sample_limit_bytes"]) {
    if (!Number.isInteger(collection[key]) || collection[key] <= 0) throw new Error(`performance ${key} 必须为正整数`);
  }
  return collection;
}

async function fingerprint(root) {
  const git = (...args) => {
    const result = Bun.spawnSync(["git", ...args], { cwd: root, stdout: "pipe", stderr: "pipe" });
    if (result.exitCode !== 0) throw new Error(`Git 指纹失败: ${new TextDecoder().decode(result.stderr)}`);
    return result.stdout;
  };
  const scope = ["app", "src", "tests", "configs", "scripts", "packages", "package.json", "pyproject.toml", "uv.lock", "bun.lock"];
  const head = new TextDecoder().decode(git("rev-parse", "HEAD")).trim();
  const diff = git("diff", "HEAD", "--binary", "--", ...scope);
  const untracked = new TextDecoder().decode(git("ls-files", "--others", "--exclude-standard", "-z", "--", ...scope)).split("\0").filter(Boolean).sort();
  const digest = createHash("sha256").update(head).update(diff);
  for (const file of untracked) {
    if (/\.(py|[cm]?js|tsx?|jsonc|json)$/.test(file)) digest.update(file).update(await readFile(path.join(root, file)));
  }
  return { head, content_sha256: digest.digest("hex"), dirty: diff.length > 0 || untracked.length > 0, scope };
}

export async function beginPerformance(root, suite, collection) {
  const started = new Date();
  const runId = process.env.BOXTEAM_TEST_RUN_ID ?? `${started.getTime()}-${process.pid}`;
  const runKey = `run-${createHash("sha1").update(runId).digest("hex").slice(0, 12)}`;
  const directory = path.join(root, "out/tests/runner/run-tests/artifacts", started.toISOString().slice(0, 10).replaceAll("-", "/"), runKey, suite.id);
  await mkdir(directory, { recursive: true });
  const before = await fingerprint(root);
  const receipt = { schema_version: 1, run_id: runId, suite_id: suite.id, layer: suite.layer,
    started_at: started.toISOString(), collection, command: redactCommand(suite.command),
    source_before: before, environment: { platform: process.platform, arch: process.arch,
      bun: Bun.version, cpu: os.cpus()[0]?.model, cpu_count: os.availableParallelism(),
      total_memory_bytes: os.totalmem(), available_memory_bytes: os.freemem() } };
  await writeFile(path.join(directory, "run.json"), `${JSON.stringify(receipt, null, 2)}\n`, { flag: "wx" });
  return { directory, receipt, clock: performance.now(), env: {
    BOXTEAM_TEST_RUN_ID: runId, BOXTEAM_TEST_RUN_STARTED_AT: started.toISOString(),
    BOXTEAM_TEST_PERF_MODE: collection.mode, BOXTEAM_TEST_PERF_DIR: directory,
    BOXTEAM_TEST_PERF_LIMIT: String(collection.sample_limit_bytes),
    BOXTEAM_TEST_PERF_TOOL: collection.tool ?? "",
  } };
}

// 命令保留结构，常见凭据参数及URL查询不进入收据。
function redactCommand(command) {
  let secret = false;
  return command.map((argument) => {
    if (secret) { secret = false; return "<redacted>"; }
    if (/^--?(api[-_]?key|token|password|authorization|secret)$/i.test(argument)) secret = true;
    return argument.replace(/([?&][^=\s]+)=([^&\s]+)/g, "$1=<redacted>")
      .replace(/((?:api[-_]?key|token|password|authorization|secret)=)[^\s]+/gi, "$1<redacted>");
  });
}

export async function tee(stream, file, output) {
  const log = createWriteStream(file, { flags: "wx" });
  const completion = finished(log);
  // 错误可能先于首个输出块到达；先接住，清理时仍原样抛出。
  completion.catch(() => {});
  try {
    for await (const chunk of stream) {
      if (log.errored) throw log.errored;
      if (!log.write(chunk)) await once(log, "drain");
      if (!output.write(chunk)) await once(output, "drain");
    }
  } finally {
    log.end();
    await completion;
  }
}

export async function finishPerformance(root, run, result) {
  const after = await fingerprint(root);
  const receipt = { ...run.receipt, ...result, elapsed_seconds: (performance.now() - run.clock) / 1000,
    source_after: after, mixed_source: after.content_sha256 !== run.receipt.source_before.content_sha256 };
  if (receipt.supervised_command) receipt.supervised_command = redactCommand(receipt.supervised_command);
  await writeFile(path.join(run.directory, "run.json"), `${JSON.stringify(receipt, null, 2)}\n`);
}
