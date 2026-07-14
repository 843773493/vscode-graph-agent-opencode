import path from "node:path";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";

export const STATE_FILE_NAME = "terminals.json";

export class TerminalStateStore {
  constructor({ workspaceRoot }) {
    this.stateDir = path.join(workspaceRoot, ".boxteam", "terminal-manager");
    this.stateFile = path.join(this.stateDir, STATE_FILE_NAME);
  }

  async readRecords() {
    await mkdir(this.stateDir, { recursive: true });
    let raw = null;
    try {
      raw = await readFile(this.stateFile, "utf8");
    } catch (error) {
      if (error?.code === "ENOENT") {
        return null;
      }
      throw error;
    }

    if (!raw.trim()) {
      return null;
    }

    const parsed = JSON.parse(raw);
    return Array.isArray(parsed?.terminals) ? parsed.terminals : [];
  }

  async write(data) {
    await mkdir(this.stateDir, { recursive: true });
    const temporaryFile = `${this.stateFile}.${process.pid}.tmp`;
    await writeFile(temporaryFile, `${JSON.stringify(data, null, 2)}\n`, "utf8");
    await rename(temporaryFile, this.stateFile);
  }
}
