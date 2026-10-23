import { spawn } from "node:child_process";
import { Readable } from "node:stream";
import path from "node:path";
import { describe, expect, test } from "bun:test";
import { HttpError, streamLimitedRequestBody } from "../src/server/http.js";

const nodeSmokeScript = path.join(import.meta.dir, "upstreamProxy.node-smoke.mjs");

describe("SSH Gateway 上游 HTTP 代理", () => {
  test("请求体流式限长，恰好达到上限时允许，超过时返回 413", async () => {
    const acceptedChunks = [];
    for await (const chunk of streamLimitedRequestBody(
      Readable.from([Buffer.from("ab"), Buffer.from("cd")]),
      4,
    )) {
      acceptedChunks.push(chunk);
    }
    expect(Buffer.concat(acceptedChunks).toString()).toBe("abcd");

    const rejectedChunks = [];
    let failure = null;
    try {
      for await (const chunk of streamLimitedRequestBody(
        Readable.from([Buffer.from("abc"), Buffer.from("de")]),
        4,
      )) {
        rejectedChunks.push(chunk);
      }
    } catch (error) {
      failure = error;
    }
    expect(failure).toBeInstanceOf(HttpError);
    expect(failure.statusCode).toBe(413);
    expect(rejectedChunks.map((chunk) => chunk.toString())).toEqual(["abc"]);
  });

  test("下游断连会取消未完成的目标响应流", async () => {
    const child = spawn("node", [nodeSmokeScript], {
      cwd: process.cwd(),
      stdio: ["ignore", "pipe", "pipe"],
    });
    let output = "";
    for (const stream of [child.stdout, child.stderr]) {
      stream.setEncoding("utf8");
      stream.on("data", (chunk) => {
        output = `${output}${chunk}`.slice(-16384);
      });
    }
    let timedOut = false;
    const timeout = setTimeout(() => {
      timedOut = true;
      child.kill();
    }, 3000);
    const childExit = new Promise((resolve, reject) => {
      child.once("error", reject);
      child.once("exit", (code, signal) => resolve({ code, signal }));
    });

    try {
      const result = await childExit;
      expect(timedOut).toBe(false);
      expect(result).toEqual({ code: 0, signal: null });
      expect(output).toContain("upstream_response_closed=true");
      expect(output).toContain("proxy_finished_without_error=true");
      expect(output).toContain("upload_streamed_before_body_complete=true");
      expect(output).toContain("oversized_body_rejected=true");
      expect(output).toContain("normal_response_completed=true");
      expect(output).toContain("connection_options_stripped=true");
    } finally {
      clearTimeout(timeout);
      if (child.exitCode === null && child.signalCode === null) {
        child.kill();
      }
    }
  });
});
