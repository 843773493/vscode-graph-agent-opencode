export function createBrowserShortcuts({
  addressInput,
  findBar,
  findInput,
  findPrevious,
  findClose,
  activePageId,
  command,
}) {
  function runFind(backwards = false) {
    const query = findInput.value;
    if (query) {
      command("find", { query, backwards });
    }
  }

  function openFind() {
    findBar.hidden = false;
    findInput.focus();
    findInput.select();
  }

  function closeFind() {
    findBar.hidden = true;
  }

  findBar.addEventListener("submit", (event) => {
    event.preventDefault();
    runFind(event.shiftKey);
  });
  findPrevious.addEventListener("click", () => runFind(true));
  findClose.addEventListener("click", closeFind);

  return function handleBrowserShortcut(event) {
    const shortcut = event.ctrlKey || event.metaKey;
    const key = event.key.toLowerCase();
    if (shortcut && key === "l") {
      addressInput.focus();
      addressInput.select();
      return true;
    }
    if ((shortcut && key === "r") || event.key === "F5") {
      command("reload");
      return true;
    }
    if (shortcut && key === "t") {
      command("newPage");
      return true;
    }
    if (shortcut && key === "w") {
      const pageId = activePageId();
      if (pageId) {
        command("closePage", { pageId });
      }
      return true;
    }
    if (shortcut && key === "f") {
      openFind();
      return true;
    }
    if (event.altKey && event.key === "ArrowLeft") {
      command("back");
      return true;
    }
    if (event.altKey && event.key === "ArrowRight") {
      command("forward");
      return true;
    }
    if (event.key === "Escape" && !findBar.hidden) {
      closeFind();
      return true;
    }
    return false;
  };
}
