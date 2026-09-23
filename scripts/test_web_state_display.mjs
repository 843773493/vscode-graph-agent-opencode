import { spawnSync } from "node:child_process";

const testFiles = [
  "./src/state/timeline/skillDisplayFlow.test.ts",
  "./src/state/requestLogDisplay/requestLogDisplay.test.ts",
  "./src/state/display/agentStateDisplay.test.ts",
  "./src/state/display/eventQueueDisplay.test.ts",
  "./src/state/trace/chatResponseParts.test.ts",
  "./src/state/tokenUsage.test.ts",
  "./src/state/session/sessionTree.test.ts",
  "./src/utils/workspaceFileReferences.test.ts",
  "./src/state/gatewayWorkspaceState.test.ts",
  "./src/state/workspaceInformation.test.ts",
  "./src/state/tests/workspaceTree.test.ts",
];

const result = spawnSync(process.execPath, ["test", ...testFiles], {
  cwd: process.cwd(),
  stdio: "inherit",
});

if (result.error) {
  throw result.error;
}
if (result.status !== 0) {
  process.exit(result.status ?? 1);
}
