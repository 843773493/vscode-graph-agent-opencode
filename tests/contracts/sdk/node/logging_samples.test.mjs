import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import path from "node:path";
import { formatRuntimeLogRecord, loadRuntimeLogContract } from "@boxteam/plugin-sdk/logging";

const contract = loadRuntimeLogContract();
const samples = JSON.parse(readFileSync(path.resolve("resources/logging/record-samples.json"), "utf8"));

for (const pretty of [true, false]) {
  test(`Python 与 JS 共用完整记录样本：pretty=${pretty}`, () => {
    for (const sample of samples) {
      expect(formatRuntimeLogRecord(sample.record, pretty, contract)).toBe(sample[pretty ? "pretty" : "compact"]);
    }
  });
}
