import {
  installTerminalPasteGuard,
  installTerminalShortcuts,
} from "./terminalShortcuts.js";

function searchOptions(incremental = false) {
  return {
    incremental,
    decorations: {
      matchBackground: "#334155",
      matchOverviewRuler: "#64748b",
      activeMatchBackground: "#b45309",
      activeMatchColorOverviewRuler: "#f59e0b",
    },
  };
}

export function installTerminalUserActions({
  terminal,
  searchAddon,
  elements,
  getAttached,
  setStatus,
  resizeTerminal,
  navigatorObject = navigator,
}) {
  const {
    searchButton,
    searchBar,
    searchInput,
    searchResult,
    searchPrevious,
    searchNext,
    searchClose,
  } = elements;
  const platform = navigatorObject.userAgentData?.platform
    || navigatorObject.platform
    || "";
  const shortcutPlatform = /mac/i.test(platform)
    ? "mac"
    : /win/i.test(platform)
      ? "windows"
      : "linux";

  function updateSearch(direction = "next", incremental = false) {
    const query = searchInput.value;
    if (!query) {
      searchAddon.clearDecorations();
      searchResult.textContent = "";
      return false;
    }
    return direction === "previous"
      ? searchAddon.findPrevious(query, searchOptions())
      : searchAddon.findNext(query, searchOptions(incremental));
  }

  function openSearch() {
    searchBar.hidden = false;
    searchInput.focus();
    searchInput.select();
    resizeTerminal();
    if (searchInput.value) {
      updateSearch("next", true);
    }
  }

  function closeSearch() {
    searchAddon.clearDecorations();
    searchBar.hidden = true;
    searchResult.textContent = "";
    terminal.focus();
    resizeTerminal();
  }

  installTerminalShortcuts({
    terminal,
    platform: shortcutPlatform,
    actions: {
      selectAll: () => {
        terminal.selectAll();
        setStatus("已选择全部终端内容");
      },
      search: openSearch,
    },
  });
  installTerminalPasteGuard({
    terminal,
    getAttached,
    onBlocked: () => setStatus("终端当前未连接，无法粘贴", true),
  });

  searchAddon.onDidChangeResults(({ resultIndex, resultCount }) => {
    searchResult.textContent = resultCount > 0
      ? `${resultIndex >= 0 ? resultIndex + 1 : "?"}/${resultCount}`
      : "无结果";
  });
  searchButton.addEventListener("click", openSearch);
  searchInput.addEventListener("input", () => {
    updateSearch("next", true);
  });
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeSearch();
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      updateSearch(event.shiftKey ? "previous" : "next");
    }
  });
  searchPrevious.addEventListener("click", () => {
    updateSearch("previous");
    searchInput.focus();
  });
  searchNext.addEventListener("click", () => {
    updateSearch("next");
    searchInput.focus();
  });
  searchClose.addEventListener("click", closeSearch);
}
