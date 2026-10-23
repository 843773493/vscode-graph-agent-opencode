import { EventEmitter } from "node:events";
import { StringDecoder } from "node:string_decoder";
import { describe, expect, test } from "bun:test";
import { pipePrefixedOutput } from "../scripts/processOutput.js";

class SourceStreamFixture extends EventEmitter {
  paused = false;
  pauseCalls = 0;
  resumeCalls = 0;
  decoder = null;

  setEncoding(encoding) {
    this.encoding = encoding;
    this.decoder = new StringDecoder(encoding);
  }

  emitChunk(chunk) {
    const text = this.decoder.write(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    if (text) {
      this.emit("data", text);
    }
  }

  end() {
    const finalText = this.decoder.end();
    if (finalText) {
      this.emit("data", finalText);
    }
    this.emit("end");
  }

  pause() {
    this.paused = true;
    this.pauseCalls += 1;
  }

  resume() {
    this.paused = false;
    this.resumeCalls += 1;
  }
}

class OutputStreamFixture extends EventEmitter {
  chunks = [];
  blockedOnce = true;

  write(chunk) {
    this.chunks.push(chunk);
    if (this.blockedOnce) {
      this.blockedOnce = false;
      return false;
    }
    return true;
  }
}

describe("角色日志转发", () => {
  test("长行按有界片段输出并遵守输出流背压", () => {
    const sourceStream = new SourceStreamFixture();
    const outputStream = new OutputStreamFixture();
    const longLine = "x".repeat(9000);
    pipePrefixedOutput(sourceStream, "gateway", outputStream);

    sourceStream.emitChunk(Buffer.from(`${longLine}\nfinal`));
    expect(outputStream.chunks).toHaveLength(1);
    expect(outputStream.chunks[0]).toBe(
      `[gateway] ${"x".repeat(8192)}\n[gateway] ${"x".repeat(808)}\n`,
    );
    expect(sourceStream.paused).toBe(true);
    expect(sourceStream.pauseCalls).toBe(1);

    outputStream.emit("drain");
    expect(sourceStream.paused).toBe(false);
    expect(sourceStream.resumeCalls).toBe(1);
    sourceStream.end();
    expect(outputStream.chunks.join("")).toBe(
      `[gateway] ${"x".repeat(8192)}\n[gateway] ${"x".repeat(808)}\n[gateway] final\n`,
    );
  });

  test("UTF-8 字节跨 chunk 且日志分片时保留完整 Unicode 字符", () => {
    const sourceStream = new SourceStreamFixture();
    const outputStream = new OutputStreamFixture();
    outputStream.blockedOnce = false;
    const fillerText = "a".repeat(8191);
    pipePrefixedOutput(sourceStream, "gateway", outputStream);

    const emojiBytes = Buffer.from("🙂");
    sourceStream.emitChunk(Buffer.concat([
      Buffer.from(fillerText),
      emojiBytes.subarray(0, 2),
    ]));
    sourceStream.emitChunk(emojiBytes.subarray(2));
    sourceStream.end();

    expect(outputStream.chunks.join("")).toBe(
      `[gateway] ${fillerText}\n[gateway] 🙂\n`,
    );
    expect(outputStream.chunks.join("")).not.toContain("\uFFFD");
  });
});
