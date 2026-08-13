import type { WorkspaceFileDownloadRequest } from "../api";

export interface FileTransferHost {
  downloadWorkspaceFile(request: WorkspaceFileDownloadRequest): Promise<void>;
}

declare global {
  interface Window {
    boxteamFileTransferHost?: FileTransferHost;
  }
}

function filenameFromDisposition(value: string | null, fallback: string): string {
  if (!value) {
    return fallback;
  }
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(value)?.[1];
  if (encoded) {
    return decodeURIComponent(encoded);
  }
  return /filename="([^"]+)"/i.exec(value)?.[1] ?? fallback;
}

export const browserFileTransferHost: FileTransferHost = {
  async downloadWorkspaceFile(request) {
    const response = await fetch(request.url, { headers: request.headers });
    if (!response.ok) {
      const payload = await response.clone().json().catch(() => null) as {
        detail?: string;
      } | null;
      throw new Error(
        `下载工作区文件失败: ${payload?.detail ?? `HTTP ${response.status}`}`,
      );
    }
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filenameFromDisposition(
      response.headers.get("Content-Disposition"),
      request.suggestedName,
    );
    anchor.style.display = "none";
    document.body.appendChild(anchor);
    try {
      anchor.click();
    } finally {
      anchor.remove();
      globalThis.setTimeout(() => URL.revokeObjectURL(objectUrl), 30_000);
    }
  },
};

export function getFileTransferHost(): FileTransferHost {
  return window.boxteamFileTransferHost ?? browserFileTransferHost;
}

export function filesFromClipboardData(
  clipboardData: Pick<DataTransfer, "files"> | null,
): File[] {
  return Array.from(clipboardData?.files ?? []);
}
