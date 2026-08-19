function participantColor(id) {
  let hash = 0;
  for (const character of id) {
    hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  }
  return `hsl(${hash % 360} 72% 52%)`;
}

export function createBrowserCollaborationUi({
  canvas,
  keyboardTarget,
  cursorLayer,
  contextMenu,
  command,
  sendIfAttached,
  setStatus,
  getRemoteViewport = () => ({ width: canvas.width, height: canvas.height }),
}) {
  const cursorTimers = new Map();

  function showContextMenu(event) {
    contextMenu.hidden = false;
    const width = contextMenu.offsetWidth;
    const height = contextMenu.offsetHeight;
    contextMenu.style.left = `${Math.max(4, Math.min(event.clientX, window.innerWidth - width - 4))}px`;
    contextMenu.style.top = `${Math.max(4, Math.min(event.clientY, window.innerHeight - height - 4))}px`;
  }

  function hideContextMenu() {
    contextMenu.hidden = true;
  }

  function showParticipantPointer(pointer) {
    const viewport = getRemoteViewport();
    let cursor = cursorLayer.querySelector(`[data-participant-id="${CSS.escape(pointer.participantId)}"]`);
    if (!cursor) {
      cursor = document.createElement("span");
      cursor.className = "participant-cursor";
      cursor.dataset.participantId = pointer.participantId;
      cursor.style.setProperty("--participant-color", participantColor(pointer.participantId));
      cursorLayer.append(cursor);
    }
    cursor.style.left = `${pointer.x / viewport.width * 100}%`;
    cursor.style.top = `${pointer.y / viewport.height * 100}%`;
    window.clearTimeout(cursorTimers.get(pointer.participantId));
    cursorTimers.set(pointer.participantId, window.setTimeout(() => {
      cursor.remove();
      cursorTimers.delete(pointer.participantId);
    }, 2500));
  }

  async function copyText(text) {
    if (!text) {
      setStatus("远程页面当前没有选中文字", true);
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // TODO: 非安全上下文仍需兼容不提供 Clipboard API 的本地浏览器。
      keyboardTarget.value = text;
      keyboardTarget.select();
      if (!document.execCommand("copy")) {
        keyboardTarget.value = "";
        throw new Error("浏览器拒绝写入本地剪贴板");
      }
      keyboardTarget.value = "";
      keyboardTarget.focus({ preventScroll: true });
    }
    setStatus(`已复制 ${text.length} 个字符到本地剪贴板`);
  }

  contextMenu.addEventListener("click", (event) => {
    const button = event.target.closest("[data-context-command]");
    if (!button) {
      return;
    }
    const action = button.dataset.contextCommand;
    hideContextMenu();
    if (["back", "forward", "reload", "newPage"].includes(action)) {
      command(action);
      return;
    }
    if (action === "copy") {
      sendIfAttached({ type: "readClipboard" });
      return;
    }
    if (action === "paste") {
      void navigator.clipboard.readText()
        .then((text) => sendIfAttached({ type: "paste", text }))
        .catch((error) => setStatus(`读取本地剪贴板失败: ${error}`, true));
    }
  });

  document.addEventListener("pointerdown", (event) => {
    if (!contextMenu.hidden && !contextMenu.contains(event.target)) {
      hideContextMenu();
    }
  });

  return { copyText, showContextMenu, showParticipantPointer };
}
