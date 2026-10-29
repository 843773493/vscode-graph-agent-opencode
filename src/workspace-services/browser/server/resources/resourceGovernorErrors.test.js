import { inspect } from "node:util";
import { describe, expect, spyOn, test } from "bun:test";
import { BrowserResourceGovernor } from "./resourceGovernor.js";

describe.each(["snapshot", "freeze", "discard"])("单项%s失败的公开事实与原始诊断", (stage) => {
  test.each([
    "/private/browser/runtime",
    String.raw`C:\private\browser\runtime`,
    String.raw`\\host\share\browser\runtime`,
  ])("保留其它资源进展、原错误码及退避: %s", async (nativePath) => {
    const cause = Object.assign(new Error(`EACCES: ${nativePath}`, {
      cause: new Error(`资源失败原始原因: ${nativePath}`),
    }), { code: "EACCES" });
    const attempted = [];
    const initialState = stage === "discard" ? "frozen" : "background";
    const sessions = ["browser_failed", "browser_healthy"].map((id) => ({
      id,
      record: { resource_state: initialState, resource_policy: "automatic" },
      resourcePolicySnapshot() {
        if (id === "browser_failed" && stage === "snapshot") throw cause;
        return {
          browser_id: id, resource_state: this.record.resource_state, resource_policy: "automatic",
          resource_protections: [], client_count: 0, created_at: "2026-01-01T00:00:00.000Z",
          frozen_at: "2026-01-01T00:00:00.000Z",
        };
      },
      async freeze() {
        attempted.push([id, "freeze"]);
        if (id === "browser_failed" && stage === "freeze") throw cause;
        this.record.resource_state = "frozen";
        return { resource_state: "frozen" };
      },
      async discard() {
        attempted.push([id, "discard"]);
        if (id === "browser_failed" && stage === "discard") throw cause;
        this.record.resource_state = "discarded";
        return { resource_state: "discarded" };
      },
    }));
    const governor = new BrowserResourceGovernor({
      manager: { runningSessions: () => sessions, get: id => sessions.find(session => session.id === id) },
      memoryMonitor: { sample: async () => ({ level: "critical" }) },
      now: () => Date.parse("2026-01-01T01:00:00.000Z"),
    });
    governor.running = true;
    governor.state.running = true;
    const logged = spyOn(process.stderr, "write").mockImplementation(() => undefined);
    try {
      const state = await governor.runCycle();
      const [failure] = state.recent_action_errors;
      expect(state.recent_action_errors).toHaveLength(1);
      expect(state.running).toBe(true);
      expect(state.error).toBeNull();
      expect(failure).toMatchObject({ browser_id: "browser_failed", action: stage, error_code: "EACCES" });
      expect(failure.message).toContain(`stage=resource_${stage}`);
      expect(failure.message).toContain("error=Error");
      expect(state.last_cycle).toMatchObject({
        completed_action_count: 1, total_failure_count: 1,
        snapshot_failure_count: stage === "snapshot" ? 1 : 0,
        failed_action_count: stage === "snapshot" ? 0 : 1,
        action_backoff_count: stage === "snapshot" ? 0 : 1,
      });
      expect(sessions[0].record.resource_state).toBe(initialState);
      expect(attempted).toContainEqual(["browser_healthy", stage === "discard" ? "discard" : "freeze"]);
      expect(state.last_actions[0].browserId).toBe("browser_healthy");
      if (stage !== "snapshot") {
        expect(governor.actionRetryAfterUnixMs.get("browser_failed")).toBe(governor.now() + 5_000);
      }
      const serialized = JSON.stringify(state);
      expect(serialized).not.toContain(JSON.stringify(nativePath).slice(1, -1));
      expect(serialized).not.toContain(".js:");
      const diagnostics = logged.mock.calls.map(args => args[0]).join("\n");
      expect(diagnostics).toContain(inspect(cause));
      expect(diagnostics).toContain("[cause]:");
      expect(cause.message).toBe(`EACCES: ${nativePath}`);
    } finally {
      governor.stop();
      logged.mockRestore();
    }
  });
});
