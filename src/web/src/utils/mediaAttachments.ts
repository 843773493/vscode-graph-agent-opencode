import type { AttachmentRef } from "../types/backend";

export type AttachmentMediaKind = "image" | "video";

export type SelectedAttachment = AttachmentRef & {
  mediaKind: AttachmentMediaKind;
  previewUrl: string;
};

export const MEDIA_ONLY_PROMPT = "请查看这个附件并根据附件内容回应。";

const VIDEO_EXTENSION_CONTENT_TYPES: Record<string, string> = {
  ".mp4": "video/mp4",
  ".webm": "video/webm",
  ".mov": "video/quicktime",
  ".mkv": "video/x-matroska",
};

const SUPPORTED_VIDEO_CONTENT_TYPES = new Set(
  Object.values(VIDEO_EXTENSION_CONTENT_TYPES),
);

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result !== "string") {
        reject(new Error("附件读取结果不是 data URL"));
        return;
      }
      resolve(reader.result);
    };
    reader.onerror = () => {
      reject(reader.error ?? new Error("附件读取失败"));
    };
    reader.readAsDataURL(file);
  });
}

function normalizeDataUrlContentType(dataUrl: string, contentType: string): string {
  const commaIndex = dataUrl.indexOf(",");
  if (!dataUrl.startsWith("data:") || commaIndex === -1) {
    throw new Error("附件读取结果不是合法 data URL");
  }

  const header = dataUrl.slice(0, commaIndex);
  const payload = dataUrl.slice(commaIndex + 1);
  const encoding = header.includes(";base64") ? ";base64" : "";
  return `data:${contentType}${encoding},${payload}`;
}

function extensionFromName(name: string): string {
  const match = name.toLowerCase().match(/\.[^.]+$/);
  return match?.[0] ?? "";
}

function fallbackContentType(file: File, mediaKind: AttachmentMediaKind): string {
  if (mediaKind === "image" && file.type) {
    return file.type;
  }

  if (mediaKind === "video") {
    if (SUPPORTED_VIDEO_CONTENT_TYPES.has(file.type)) {
      return file.type;
    }

    const byExtension = VIDEO_EXTENSION_CONTENT_TYPES[extensionFromName(file.name)];
    if (byExtension) {
      return byExtension;
    }
  }

  return mediaKind === "image" ? "image/png" : "video/mp4";
}

function detectMediaKind(file: File): AttachmentMediaKind {
  if (file.type.startsWith("image/")) {
    return "image";
  }
  if (file.type.startsWith("video/")) {
    if (SUPPORTED_VIDEO_CONTENT_TYPES.has(file.type)) {
      return "video";
    }

    const extension = extensionFromName(file.name);
    if (extension in VIDEO_EXTENSION_CONTENT_TYPES) {
      return "video";
    }

    throw new Error("仅支持 mp4、webm、mov 或 mkv 视频附件");
  }

  const extension = extensionFromName(file.name);
  if (extension in VIDEO_EXTENSION_CONTENT_TYPES) {
    return "video";
  }

  throw new Error("仅支持图片或视频附件");
}

function generatedAttachmentName(
  file: File,
  mediaKind: AttachmentMediaKind,
  index: number,
): string {
  if (file.name) {
    return file.name;
  }

  const contentType = fallbackContentType(file, mediaKind);
  const extension = contentType.split("/")[1] || (mediaKind === "image" ? "png" : "mp4");
  const timestamp = new Date()
    .toISOString()
    .replace(/:/g, "")
    .replace(/\.\d{3}Z$/, "Z");
  return `clipboard-${mediaKind}-${timestamp}-${index + 1}.${extension}`;
}

function inlineAttachmentFileId(name: string, index: number): string {
  const uniqueId =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${index}`;
  return `inline:${uniqueId}:${name}`;
}

export async function fileToSelectedAttachment(
  file: File,
  fallbackIndex = 0,
): Promise<SelectedAttachment> {
  const mediaKind = detectMediaKind(file);
  const name = generatedAttachmentName(file, mediaKind, fallbackIndex);
  const contentType = fallbackContentType(file, mediaKind);
  const dataUrl = normalizeDataUrlContentType(
    await readFileAsDataUrl(file),
    contentType,
  );
  return {
    file_id: inlineAttachmentFileId(name, fallbackIndex),
    name,
    content_type: contentType,
    data_url: dataUrl,
    mediaKind,
    previewUrl: dataUrl,
  };
}

function isSupportedClipboardMedia(item: DataTransferItem): boolean {
  return item.kind === "file" && (
    item.type.startsWith("image/") ||
    item.type.startsWith("video/")
  );
}

function isSupportedFile(file: File): boolean {
  try {
    detectMediaKind(file);
    return true;
  } catch {
    return false;
  }
}

export function mediaFilesFromClipboard(data: DataTransfer): File[] {
  const files = Array.from(data.files).filter(isSupportedFile);
  if (files.length > 0) {
    return files;
  }

  return Array.from(data.items)
    .filter(isSupportedClipboardMedia)
    .map((item) => item.getAsFile())
    .filter((file): file is File => file !== null);
}
