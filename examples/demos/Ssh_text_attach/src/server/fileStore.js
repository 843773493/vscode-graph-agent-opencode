import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";

export class TextFileStore {
  constructor({ filePath, label }) {
    if (!filePath) {
      throw new Error("TextFileStore 缺少 filePath");
    }
    if (!label) {
      throw new Error("TextFileStore 缺少 label");
    }
    this.filePath = path.resolve(filePath);
    this.label = label;
  }

  initialContent() {
    return `${this.label}\n\n这是 Ssh_text_attach demo 管理的 txt 文件。\n首次创建: ${new Date().toISOString()}\n`;
  }

  async ensureReady() {
    await mkdir(path.dirname(this.filePath), { recursive: true });
    try {
      await stat(this.filePath);
    } catch (error) {
      if (error?.code !== "ENOENT") {
        throw error;
      }
      await writeFile(this.filePath, this.initialContent(), "utf8");
    }
  }

  async snapshot() {
    await this.ensureReady();
    const [content, metadata] = await Promise.all([
      readFile(this.filePath, "utf8"),
      stat(this.filePath),
    ]);
    return {
      path: this.filePath,
      content,
      updatedAt: metadata.mtime.toISOString(),
      bytes: Buffer.byteLength(content, "utf8"),
    };
  }

  async save(content) {
    if (typeof content !== "string") {
      throw new Error("保存内容必须是字符串");
    }
    await this.ensureReady();
    await writeFile(this.filePath, content, "utf8");
    return this.snapshot();
  }
}
