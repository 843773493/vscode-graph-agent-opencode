import { existsSync } from "node:fs";

export function rebuildWindowsNodePty({
  applicationRoot,
  nodeExecutable,
  nodeGypEntrypoint,
  run,
  exists = existsSync,
}) {
  const driveLetter = [..."ZYXWVUTSRQPONMLKJIHGFED"].find(
    (letter) => !exists(`${letter}:\\`),
  );
  if (!driveLetter) throw new Error("没有可用于 Windows node-pty 构建的临时盘符");

  const drive = `${driveLetter}:`;
  run("subst.exe", [drive, applicationRoot]);
  try {
    run(
      nodeExecutable,
      [nodeGypEntrypoint, "rebuild"],
      { cwd: `${drive}\\node_modules\\node-pty` },
    );
  } finally {
    run("subst.exe", [drive, "/d"]);
  }
}
