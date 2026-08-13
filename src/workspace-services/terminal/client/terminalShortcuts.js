export function terminalShortcutAction(event, { platform, hasSelection }) {
  if (event.type !== "keydown") {
    return null;
  }
  const key = event.key.toLowerCase();
  if (platform === "mac") {
    if (!event.metaKey || event.ctrlKey || event.altKey) {
      return null;
    }
    if (key === "c") return "copy";
    if (key === "v") return "paste";
    if (key === "a") return "selectAll";
    if (key === "f") return "search";
    return null;
  }

  if (platform === "windows") {
    if (event.ctrlKey && !event.shiftKey && !event.altKey) {
      if (key === "c" && hasSelection) return "copy";
      if (key === "v") return "paste";
      if (key === "f") return "search";
    }
    return null;
  }

  if (event.ctrlKey && !event.altKey) {
    if (event.shiftKey && key === "c") return "copy";
    if (event.shiftKey && key === "v") return "paste";
    if (!event.shiftKey && key === "f") return "search";
  }
  if (event.shiftKey && !event.ctrlKey && !event.altKey && event.key === "Insert") {
    return "paste";
  }
  return null;
}

export function installTerminalShortcuts({ terminal, platform, actions }) {
  if (!terminal.element) {
    throw new Error("终端尚未挂载，无法安装快捷键");
  }
  terminal.element.addEventListener("keydown", (event) => {
    const action = terminalShortcutAction(event, {
      platform,
      hasSelection: terminal.hasSelection(),
    });
    if (action === "copy" || action === "paste") {
      event.stopPropagation();
    }
  }, true);
  terminal.attachCustomKeyEventHandler((event) => {
    const action = terminalShortcutAction(event, {
      platform,
      hasSelection: terminal.hasSelection(),
    });
    if (!action) {
      return true;
    }
    if (action === "copy" || action === "paste") {
      return false;
    }
    event.preventDefault();
    event.stopPropagation();
    actions[action]();
    return false;
  });
}

export function installTerminalPasteGuard({ terminal, getAttached, onBlocked }) {
  if (!terminal.element) {
    throw new Error("终端尚未挂载，无法安装粘贴保护");
  }
  terminal.element.addEventListener("paste", (event) => {
    if (getAttached()) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    onBlocked();
    terminal.focus();
  }, true);
}
