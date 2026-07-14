import path from "node:path";
import { mkdir, readFile, writeFile } from "node:fs/promises";

export class BrowserStateStore {
  constructor({ workspaceRoot }) {
    this.workspaceRoot = path.resolve(workspaceRoot);
    this.stateDir = path.join(this.workspaceRoot, ".boxteam", "browser-manager");
    this.screenshotDir = path.join(this.stateDir, "screenshots");
    this.stateFile = path.join(this.stateDir, "browsers.json");
  }

  async readRecords() {
    try {
      const raw = await readFile(this.stateFile, "utf8");
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed.browsers)) {
        throw new Error(`浏览器状态文件格式错误: ${this.stateFile}`);
      }
      return parsed.browsers;
    } catch (error) {
      if (error?.code === "ENOENT") {
        return null;
      }
      throw error;
    }
  }

  async write(payload) {
    await mkdir(this.stateDir, { recursive: true });
    await writeFile(this.stateFile, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  }

  async writeScreenshot(browserId, buffer) {
    await mkdir(this.screenshotDir, { recursive: true });
    const fileName = `${browserId}-${Date.now()}.png`;
    const filePath = path.join(this.screenshotDir, fileName);
    await writeFile(filePath, buffer);
    return filePath;
  }
}
