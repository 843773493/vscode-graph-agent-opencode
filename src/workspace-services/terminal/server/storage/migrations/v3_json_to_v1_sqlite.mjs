import path from "node:path";
import { parseArgs } from "node:util";
import { TerminalStateStore } from "../../terminalStateStore.js";
import { validateWorkspaceId, verifyWorkspaceIdentity } from "../../workspaceIdentity.js";

const { values } = parseArgs({ options: {
  workspace: { type: "string" }, "workspace-id": { type: "string" },
} });
if (!values.workspace || !values["workspace-id"] || !path.isAbsolute(values.workspace)) {
  throw new Error("用法：node src/workspace-services/terminal/server/storage/migrations/v3_json_to_v1_sqlite.mjs --workspace <绝对工作区路径> --workspace-id <UUID>；先停止该工作区的终端服务");
}
const workspaceId = validateWorkspaceId(values["workspace-id"]);
await verifyWorkspaceIdentity(values.workspace, workspaceId);
const store = new TerminalStateStore({ workspaceRoot: values.workspace, workspaceId });
try {
  const result = await store.request("import");
  process.stdout.write(`${JSON.stringify({ workspace_id: workspaceId, ...result, database: store.stateFile })}\n`);
} finally {
  await store.close();
}
