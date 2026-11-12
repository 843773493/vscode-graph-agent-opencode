import { AsyncLocalStorage } from "node:async_hooks";
import { randomUUID } from "node:crypto";
import { mkdirSync, openSync, writeSync, closeSync, writeFileSync } from "node:fs";
import path from "node:path";

const parent = new AsyncLocalStorage();

export class Perf {
  constructor(context, { mode = process.env.BOXTEAM_TEST_PERF_MODE ?? "basic" } = {}) {
    this.mode = mode;
    this.directory = context.performanceDir;
    this.identity = { schema_version: 1, run_id: process.env.BOXTEAM_TEST_RUN_ID ?? path.basename(context.runRoot),
      suite_id: process.env.BOXTEAM_TEST_SUITE ?? null, test_id: process.env.PYTEST_CURRENT_TEST?.replace(/ \((setup|call|teardown)\)$/, "") ?? context.testId,
      worker_id: process.env.PYTEST_XDIST_WORKER ?? "main", pid: process.pid, clock: "performance.now" };
    this.bytes = 0;
    this.dropped = 0;
    this.limit = Number(process.env.BOXTEAM_TEST_PERF_LIMIT ?? 64 * 1024 * 1024);
    if (this.mode !== "off") {
      mkdirSync(this.directory, { recursive: true });
      this.path = path.join(this.directory, `performance-${process.pid}-${randomUUID()}.jsonl`);
      this.fd = openSync(this.path, "wx");
      const suiteDir = process.env.BOXTEAM_TEST_PERF_DIR;
      try {
        if (suiteDir) {
          mkdirSync(path.join(suiteDir, "links"), { recursive: true });
          writeFileSync(path.join(suiteDir, "links", `${randomUUID()}.json`), JSON.stringify({ path: path.relative(context.projectRoot, this.path) }));
        }
      } catch (error) { closeSync(this.fd); this.fd = undefined; throw error; }
    }
    context.addCleanup("性能记录", () => this.close());
  }

  record(event, stage, fields = {}, workload = {}) {
    if (this.fd === undefined) return;
    for (const value of [...Object.values(fields), ...Object.values(workload)]) {
      if (value !== null && !["string", "number", "boolean"].includes(typeof value)) throw new TypeError("性能记录只接受标量");
    }
    const line = JSON.stringify({ ...this.identity, event, stage, monotonic_ns: Math.round(performance.now() * 1e6), workload, ...fields }) + "\n";
    const bytes = Buffer.byteLength(line);
    if (this.bytes + bytes > this.limit) { this.dropped++; return; }
    writeSync(this.fd, line);
    this.bytes += bytes;
  }

  async stage(name, workload, action) {
    if (this.mode === "off") return await action({});
    const identity = randomUUID();
    const fields = { stage_id: identity, parent_stage_id: parent.getStore() ?? null, inclusive: true };
    const start = performance.now();
    const result = {};
    let status = "passed";
    let failure;
    this.record("start", name, fields, workload);
    return await parent.run(identity, async () => {
      try { return await action(result); }
      catch (error) { failure = error; status = error.name === "AbortError" ? "cancelled" : "failed"; result.error_type = error.name; throw error; }
      finally {
        try { this.record("end", name, { ...fields, ...result, status, elapsed_ns: Math.round((performance.now() - start) * 1e6) }, workload); }
        catch (captureError) {
          if (!failure) throw captureError;
          console.error("性能阶段完成记录失败:", captureError);
        }
      }
    });
  }

  close() {
    if (this.fd !== undefined) {
      const fd = this.fd;
      this.fd = undefined;
      try { writeSync(fd, JSON.stringify({ ...this.identity, event: "closed", stage: "collection", dropped_records: this.dropped }) + "\n"); }
      finally { closeSync(fd); }
    }
  }
}
