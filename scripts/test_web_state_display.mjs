import { spawnSync } from "node:child_process";

const testFiles = [
  "./src/state/tests/skillDisplayFlow.test.ts",
  "./src/state/tests/requestLogDisplay.test.ts",
  "./src/state/tests/agentStateDisplay.test.ts",
  "./src/state/tests/eventQueueDisplay.test.ts",
];

for (const testFile of testFiles) {
  const result = spawnSync(process.execPath, [testFile], {
    cwd: process.cwd(),
    stdio: "inherit",
  });

  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}
