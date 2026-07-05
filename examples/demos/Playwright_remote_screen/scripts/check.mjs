import { spawnSync } from "node:child_process";

const files = [
  "src/server/server.js",
  "src/server/browserSession.js",
  "src/server/protocol.js",
  "src/server/terminalCommands.js",
  "src/server/url.js",
  "src/server/commandClient.js",
  "src/client/main.js",
  "scripts/check.mjs",
];

for (const file of files) {
  const result = spawnSync(process.execPath, ["--check", file], {
    stdio: "inherit",
  });
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}
