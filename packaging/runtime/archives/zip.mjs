import { createWriteStream, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { pipeline } from "node:stream/promises";
import extract from "@electron-internal/extract-zip";
import { ZipFile } from "yazl";

export { extract as extractZipArchive };

export async function createZipArchive(sourceDirectory, archivePath) {
  const zip = new ZipFile();
  zip.on("error", (error) => zip.outputStream.destroy(error));
  const writing = pipeline(zip.outputStream, createWriteStream(archivePath));
  function addEntry(source, name) {
    // 与原 Linux zip -r 一致：将链接目标的内容装入便携版。
    const metadata = statSync(source);
    if (metadata.isDirectory()) {
      zip.addEmptyDirectory(name, { mode: metadata.mode, mtime: metadata.mtime });
      for (const entry of readdirSync(source)) {
        addEntry(path.join(source, entry), path.posix.join(name, entry));
      }
    } else if (metadata.isFile()) {
      zip.addFile(source, name);
    } else {
      throw new Error(`ZIP 不支持此文件类型: ${source}`);
    }
  }
  try {
    addEntry(sourceDirectory, path.basename(sourceDirectory));
    zip.end();
  } catch (error) {
    zip.emit("error", error);
  }
  await writing;
}
