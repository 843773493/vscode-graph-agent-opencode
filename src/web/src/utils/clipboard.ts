function fallbackCopyText(text: string): void {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.top = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  // TODO: 兼容非安全上下文下 Clipboard API 不可用的浏览器，后续全站 HTTPS 后移除。
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) {
    throw new Error("浏览器拒绝复制文本");
  }
}

export async function copyTextToClipboard(text: string): Promise<void> {
  let clipboardError: unknown = null;
  if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      lastWrittenText = text;
      return;
    } catch (error) {
      clipboardError = error;
    }
  }

  try {
    fallbackCopyText(text);
    lastWrittenText = text;
  } catch (fallbackError) {
    if (clipboardError) {
      const clipboardMessage = clipboardError instanceof Error
        ? clipboardError.message
        : String(clipboardError);
      const fallbackMessage = fallbackError instanceof Error
        ? fallbackError.message
        : String(fallbackError);
      throw new Error(
        `Clipboard API 失败：${clipboardMessage}；兼容复制失败：${fallbackMessage}`,
      );
    }
    throw fallbackError;
  }
}

export async function readTextFromClipboard(): Promise<string> {
  let clipboardError: unknown = null;
  if (navigator.clipboard?.readText) {
    try {
      const text = (await navigator.clipboard.readText()).trim();
      if (text) {
        return text;
      }
    } catch (error) {
      clipboardError = error;
    }
  }

  if (lastWrittenText) {
    return lastWrittenText;
  }
  if (clipboardError) {
    const message = clipboardError instanceof Error
      ? clipboardError.message
      : String(clipboardError);
    throw new Error(
      `浏览器拒绝读取剪贴板，且应用内没有最近复制的文本: ${message}`,
    );
  }
  throw new Error("剪贴板为空，且应用内没有最近复制的文本");
}

export async function readFilePathTextFromClipboard(): Promise<string> {
  let fileClipboardError: unknown = null;
  if (navigator.clipboard?.read) {
    try {
      const items = await navigator.clipboard.read();
      const preferredTypes = [
        "x-special/gnome-copied-files",
        "text/uri-list",
        "text/plain",
      ];
      for (const type of preferredTypes) {
        const item = items.find((candidate) => candidate.types.includes(type));
        if (!item) {
          continue;
        }
        const text = (await (await item.getType(type)).text()).trim();
        if (text) {
          return text;
        }
      }
    } catch (error) {
      // TODO: 浏览器统一支持文件剪贴板 MIME 后，移除纯文本和应用内副本兼容路径。
      fileClipboardError = error;
    }
  }
  try {
    return await readTextFromClipboard();
  } catch (textError) {
    if (fileClipboardError) {
      const fileMessage = fileClipboardError instanceof Error
        ? fileClipboardError.message
        : String(fileClipboardError);
      const textMessage = textError instanceof Error
        ? textError.message
        : String(textError);
      throw new Error(
        `读取文件剪贴板失败：${fileMessage}；读取文本路径失败：${textMessage}`,
      );
    }
    throw textError;
  }
}
let lastWrittenText: string | null = null;
