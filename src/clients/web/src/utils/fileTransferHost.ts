import type { WorkspaceFileDownloadRequest } from "../api";
import { requestGatewayResponse } from "../api/http";

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
    const response = await requestGatewayResponse(request.port, request.path, { headers: request.headers });
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
