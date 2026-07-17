export function bindBrowserToolbarEvents({
  browserId,
  backendBaseUrl,
  requestHeaders,
  attachToggle,
  refreshStateButton,
  backButton,
  forwardButton,
  reloadButton,
  addressInput,
  urlForm,
  closeBrowserButton,
  deleteBrowserButton,
  isAttached,
  attach,
  detach,
  loadSnapshot,
  command,
  updateControls,
  applyState,
  markDeleted,
  setStatus,
}) {
  attachToggle.addEventListener("click", () => {
    if (isAttached()) {
      detach();
    } else {
      attach();
    }
  });

  refreshStateButton.addEventListener("click", () => {
    void loadSnapshot().catch((error) => {
      setStatus(error instanceof Error ? error.message : String(error), true);
    });
  });

  backButton.addEventListener("click", () => command("back"));
  forwardButton.addEventListener("click", () => command("forward"));
  reloadButton.addEventListener("click", () => command("reload"));
  addressInput.addEventListener("input", updateControls);

  urlForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const targetUrl = addressInput.value.trim();
    if (!targetUrl) {
      setStatus("请输入要打开的 URL", true);
      return;
    }
    command("goto", { url: targetUrl });
  });

  closeBrowserButton.addEventListener("click", async () => {
    if (!browserId) {
      setStatus("URL 缺少 browserId 参数", true);
      return;
    }
    const response = await fetch(`${backendBaseUrl}/api/browsers/${encodeURIComponent(browserId)}/close`, {
      method: "POST",
      headers: requestHeaders,
    });
    if (!response.ok) {
      setStatus(`关闭失败: ${response.status}`, true);
      return;
    }
    const payload = await response.json();
    applyState(payload.data);
  });

  deleteBrowserButton.addEventListener("click", async () => {
    if (!browserId) {
      setStatus("URL 缺少 browserId 参数", true);
      return;
    }
    if (!window.confirm(`确认删除浏览器页面 ${browserId}？删除后不可再 attach。`)) {
      return;
    }
    detach();
    const response = await fetch(`${backendBaseUrl}/api/browsers/${encodeURIComponent(browserId)}`, {
      method: "DELETE",
      headers: requestHeaders,
    });
    if (!response.ok) {
      setStatus(`删除失败: ${response.status}`, true);
      return;
    }
    const payload = await response.json();
    markDeleted("浏览器页面已删除", payload.data?.browser ?? null);
  });
}
