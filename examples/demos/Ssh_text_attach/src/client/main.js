const apiOrigin = `${window.location.protocol}//${window.location.hostname}:7910`;

const elements = {
  apiOrigin: document.querySelector("#api-origin"),
  targetList: document.querySelector("#target-list"),
  activeKind: document.querySelector("#active-kind"),
  activeTitle: document.querySelector("#active-title"),
  attachButton: document.querySelector("#attach-button"),
  refreshButton: document.querySelector("#refresh-button"),
  saveButton: document.querySelector("#save-button"),
  statusText: document.querySelector("#status-text"),
  filePath: document.querySelector("#file-path"),
  updatedAt: document.querySelector("#updated-at"),
  editor: document.querySelector("#file-editor"),
  toast: document.querySelector("#toast"),
};

const state = {
  targets: [],
  selectedTargetId: null,
  attachedTargetId: null,
  file: null,
  dirty: false,
};

elements.apiOrigin.textContent = apiOrigin;

function requireElement(element, name) {
  if (!element) {
    throw new Error(`页面缺少元素: ${name}`);
  }
  return element;
}

for (const [name, element] of Object.entries(elements)) {
  requireElement(element, name);
}

function showToast(message, kind = "info") {
  elements.toast.textContent = message;
  elements.toast.className = `toast ${kind}`;
  elements.toast.hidden = false;
}

function clearToast() {
  elements.toast.hidden = true;
  elements.toast.textContent = "";
}

async function requestJson(path, options = {}) {
  const response = await fetch(`${apiOrigin}${path}`, {
    ...options,
    headers: {
      "content-type": "application/json",
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  const payload = text.trim() ? JSON.parse(text) : {};
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function selectedTarget() {
  return state.targets.find((target) => target.id === state.selectedTargetId);
}

function managedFileProxyPath(targetId) {
  return `/api/proxy/${encodeURIComponent(targetId)}/api/managed-file`;
}

function renderTargets() {
  elements.targetList.replaceChildren();
  for (const target of state.targets) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = target.id === state.selectedTargetId ? "target-item active" : "target-item";
    button.dataset.targetId = target.id;

    const title = document.createElement("strong");
    title.textContent = target.name;
    const detail = document.createElement("span");
    const endpoint = target.kind === "ssh" ? `${target.ssh.user}@${target.ssh.host}:${target.ssh.port}` : target.backendOrigin;
    detail.textContent = `${target.id} · ${endpoint}`;

    button.append(title, detail);
    button.addEventListener("click", () => {
      state.selectedTargetId = target.id;
      if (state.attachedTargetId !== target.id) {
        state.file = null;
        state.dirty = false;
      }
      render();
    });
    elements.targetList.append(button);
  }
}

function renderFile(file) {
  state.file = file;
  state.dirty = false;
  elements.editor.value = file.content;
  elements.filePath.textContent = file.path;
  elements.updatedAt.textContent = new Date(file.updatedAt).toLocaleString();
}

function render() {
  renderTargets();
  const target = selectedTarget();
  const attached = state.attachedTargetId === state.selectedTargetId && state.file;

  elements.activeTitle.textContent = target ? target.name : "选择目标";
  elements.activeKind.textContent = target ? (target.kind === "ssh" ? "SSH attach" : "Local attach") : "未 attach";
  elements.attachButton.disabled = !target;
  elements.attachButton.textContent = attached ? "重新 Attach" : "Attach";
  elements.refreshButton.disabled = !attached;
  elements.saveButton.disabled = !attached || !state.dirty;
  elements.editor.disabled = !attached;
  elements.statusText.textContent = attached ? (state.dirty ? "已修改" : "已 attach") : "未连接";

  if (!attached) {
    elements.editor.value = "";
    elements.filePath.textContent = "-";
    elements.updatedAt.textContent = "-";
  }
}

async function attachSelectedTarget() {
  const target = selectedTarget();
  if (!target) {
    throw new Error("未选择 attach 目标");
  }
  clearToast();
  elements.statusText.textContent = "正在 attach";
  const payload = await requestJson(managedFileProxyPath(target.id));
  state.attachedTargetId = target.id;
  renderFile(payload.file);
  render();
  showToast(`已 attach 到 ${target.name}`, "success");
}

async function refreshFile() {
  if (!state.attachedTargetId) {
    throw new Error("尚未 attach");
  }
  clearToast();
  const payload = await requestJson(managedFileProxyPath(state.attachedTargetId));
  renderFile(payload.file);
  render();
  showToast("已刷新", "success");
}

async function saveFile() {
  if (!state.attachedTargetId) {
    throw new Error("尚未 attach");
  }
  clearToast();
  elements.statusText.textContent = "正在保存";
  const payload = await requestJson(managedFileProxyPath(state.attachedTargetId), {
    method: "PUT",
    body: JSON.stringify({ content: elements.editor.value }),
  });
  renderFile(payload.file);
  render();
  showToast("已保存", "success");
}

async function withUiError(action) {
  try {
    await action();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    elements.statusText.textContent = "错误";
    showToast(message, "error");
    console.error(error);
  }
}

elements.attachButton.addEventListener("click", () => {
  void withUiError(attachSelectedTarget);
});

elements.refreshButton.addEventListener("click", () => {
  void withUiError(refreshFile);
});

elements.saveButton.addEventListener("click", () => {
  void withUiError(saveFile);
});

elements.editor.addEventListener("input", () => {
  if (!state.file) {
    return;
  }
  state.dirty = elements.editor.value !== state.file.content;
  render();
});

const initialTargets = await requestJson("/api/targets");
state.targets = initialTargets.targets;
state.selectedTargetId = initialTargets.defaultTargetId;
render();
