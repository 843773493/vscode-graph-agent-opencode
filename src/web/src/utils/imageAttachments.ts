import type { AttachmentRef } from "../types/backend";

export type SelectedAttachment = AttachmentRef & {
  previewUrl: string;
};

export const IMAGE_ONLY_PROMPT = "请查看这张图片并根据图片内容回应。";

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result !== "string") {
        reject(new Error("图片读取结果不是 data URL"));
        return;
      }
      resolve(reader.result);
    };
    reader.onerror = () => {
      reject(reader.error ?? new Error("图片读取失败"));
    };
    reader.readAsDataURL(file);
  });
}

function clipboardImageName(file: File, index: number): string {
  if (file.name) {
    return file.name;
  }

  const extension = file.type.split("/")[1] || "png";
  const timestamp = new Date()
    .toISOString()
    .replace(/:/g, "")
    .replace(/\.\d{3}Z$/, "Z");
  return `clipboard-image-${timestamp}-${index + 1}.${extension}`;
}

export async function fileToSelectedAttachment(
  file: File,
  fallbackIndex = 0,
): Promise<SelectedAttachment> {
  if (!file.type.startsWith("image/")) {
    throw new Error("仅支持图片附件");
  }

  const name = clipboardImageName(file, fallbackIndex);
  const dataUrl = await readFileAsDataUrl(file);
  return {
    file_id: `inline:${name}`,
    name,
    content_type: file.type || "image/png",
    data_url: dataUrl,
    previewUrl: dataUrl,
  };
}

export function imageFilesFromClipboard(data: DataTransfer): File[] {
  const files = Array.from(data.files).filter((file) =>
    file.type.startsWith("image/"),
  );
  if (files.length > 0) {
    return files;
  }

  return Array.from(data.items)
    .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
    .map((item) => item.getAsFile())
    .filter((file): file is File => file !== null);
}
