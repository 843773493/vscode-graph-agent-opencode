import assert from "node:assert/strict";
import test from "node:test";

import { splitTerminalOutputChunks, trimTerminalBuffer } from "./terminalUtf8Output.js";

test("空输出既不生成事件也不改变尾部缓冲", () => {
  assert.deepEqual(splitTerminalOutputChunks(""), []);
  assert.equal(trimTerminalBuffer(""), "");
});

test("未超限的输出原样保留，包括中文、emoji和孤立代理项", () => {
  for (const value of ["a".repeat(1024), "中文 😀\r\n", "\ud800 text \udfff"]) {
    assert.deepEqual(splitTerminalOutputChunks(value), [value]);
    assert.equal(trimTerminalBuffer(value), value);
  }
});

test("尾部缓冲恰好达到256KiB时不丢字节", () => {
  const value = "a".repeat(256 * 1024);
  assert.equal(trimTerminalBuffer(value), value);
});

test("ASCII尾部缓冲严格保留最后256KiB", () => {
  const suffix = "a".repeat(256 * 1024);
  assert.equal(trimTerminalBuffer(`discard${suffix}`), suffix);
});

test("汉字截断丢弃不足一个字符的前缀，不引入替换字符", () => {
  const input = "中".repeat(Math.floor(256 * 1024 / 3) + 1);
  const tail = trimTerminalBuffer(input);
  assert.equal(tail, input.slice(1));
  assert.equal(tail.includes("\ufffd"), false);
  assert.ok(Buffer.byteLength(tail, "utf8") <= 256 * 1024);
  assert.equal(trimTerminalBuffer(tail), tail);
});

test("emoji截断不留下半个码点，末尾ASCII仍保留", () => {
  const input = `${"😀".repeat(65537)}x`;
  const tail = trimTerminalBuffer(input);
  assert.equal(tail, `${"😀".repeat(65535)}x`);
  assert.ok(Buffer.byteLength(tail, "utf8") <= 256 * 1024);
});

test("恰好64KiB的输出只生成一个事件", () => {
  const input = "a".repeat(64 * 1024);
  assert.deepEqual(splitTerminalOutputChunks(input), [input]);
});

test("汉字跨越64KiB边界时整字符进入下一事件", () => {
  const prefix = "a".repeat(64 * 1024 - 1);
  assert.deepEqual(splitTerminalOutputChunks(`${prefix}中abc`), [prefix, "中abc"]);
});

test("emoji跨越64KiB边界时不拆代理对", () => {
  const prefix = "a".repeat(64 * 1024 - 2);
  assert.deepEqual(splitTerminalOutputChunks(`${prefix}😀`), [prefix, "😀"]);
});

test("多事件混合输出按字节有界且重拼后完整一致", () => {
  const input = "中文😀ASCII\r\n".repeat(20000);
  const chunks = splitTerminalOutputChunks(input);
  assert.ok(chunks.length > 2);
  assert.equal(chunks.join(""), input);
  for (const chunk of chunks) {
    assert.ok(Buffer.byteLength(chunk, "utf8") <= 64 * 1024);
    assert.equal(chunk.includes("\ufffd"), false);
  }
});

test("超长JavaScript字符串中的孤立代理项仍原样输出", () => {
  const input = `\ud800${"a".repeat(64 * 1024)}\udfff`;
  const chunks = splitTerminalOutputChunks(input);
  assert.equal(chunks.join(""), input);
  for (const chunk of chunks) {
    assert.ok(Buffer.byteLength(chunk, "utf8") <= 64 * 1024);
  }
});

test("双字节字符在64KiB边界按UTF-8宽度切块", () => {
  const exactPrefix = "a".repeat(64 * 1024 - 2);
  assert.deepEqual(splitTerminalOutputChunks(`${exactPrefix}é!`), [`${exactPrefix}é`, "!"]);
  const shortPrefix = "a".repeat(64 * 1024 - 1);
  assert.deepEqual(splitTerminalOutputChunks(`${shortPrefix}é!`), [shortPrefix, "é!"]);
});

test("孤立代理项在边界按3字节计费，仍保留原UTF-16值", () => {
  for (const surrogate of ["\ud800", "\udfff"]) {
    const exactPrefix = "a".repeat(64 * 1024 - 3);
    assert.deepEqual(splitTerminalOutputChunks(`${exactPrefix}${surrogate}!`), [`${exactPrefix}${surrogate}`, "!"]);
    const shortPrefix = "a".repeat(64 * 1024 - 2);
    assert.deepEqual(splitTerminalOutputChunks(`${shortPrefix}${surrogate}!`), [shortPrefix, `${surrogate}!`]);
  }
});

test("混合四类UTF-8宽度与孤立代理项跨多块时保持完整并填满每块", () => {
  const input = "aé中😀\ud800z\udfff\0".repeat(25000);
  const chunks = splitTerminalOutputChunks(input);
  assert.ok(chunks.length > 2);
  assert.equal(chunks.join(""), input);
  for (const [index, chunk] of chunks.entries()) {
    const bytes = Buffer.byteLength(chunk, "utf8");
    assert.ok(bytes > 0 && bytes <= 64 * 1024);
    if (index + 1 < chunks.length) {
      const nextCharacter = chunks[index + 1][Symbol.iterator]().next().value;
      assert.ok(bytes + Buffer.byteLength(nextCharacter, "utf8") > 64 * 1024);
    }
  }
});

test("超限尾部切入代理对或包含孤立代理项时保持UTF-8截断语义", () => {
  const limit = 256 * 1024;
  const cases = [
    [`discard😀${"a".repeat(limit - 1)}`, "a".repeat(limit - 1)],
    [`discard\ud800${"a".repeat(limit - 3)}`, `\ufffd${"a".repeat(limit - 3)}`],
    [`discard${"a".repeat(limit - 3)}\udfff`, `${"a".repeat(limit - 3)}\ufffd`],
  ];
  for (const [input, expected] of cases) {
    const tail = trimTerminalBuffer(input);
    assert.equal(tail, expected);
    assert.ok(Buffer.byteLength(tail, "utf8") <= limit);
    assert.equal(trimTerminalBuffer(tail), tail);
  }
});

test("超长输出只扫描和编码有界尾部，不把已丢弃正文转换成Buffer", () => {
  const limit = 256 * 1024;
  const expected = `${"t".repeat(limit - 4)}😀`;
  const input = `${"丢弃😀".repeat(200000)}${expected}`;
  const originalFrom = Buffer.from;
  const originalByteLength = Buffer.byteLength;
  let largestEncodedBytes = 0;
  let largestMeasuredUnits = 0;
  let tail;
  try {
    Buffer.from = (...args) => {
      const result = originalFrom(...args);
      if (typeof args[0] === "string") largestEncodedBytes = Math.max(largestEncodedBytes, result.byteLength);
      return result;
    };
    Buffer.byteLength = (value, ...args) => {
      if (typeof value === "string") largestMeasuredUnits = Math.max(largestMeasuredUnits, value.length);
      return originalByteLength(value, ...args);
    };
    tail = trimTerminalBuffer(input);
  } finally {
    Buffer.from = originalFrom;
    Buffer.byteLength = originalByteLength;
  }
  assert.equal(tail, expected);
  assert.ok(largestEncodedBytes <= 3 * limit, `单次编码分配过大: ${largestEncodedBytes}`);
  assert.ok(largestMeasuredUnits <= limit, `扫描了已丢弃正文: ${largestMeasuredUnits}`);
});
