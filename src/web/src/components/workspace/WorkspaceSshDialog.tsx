import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import type { AddSshGatewayWorkspaceRequest } from "../../types/backend";

interface WorkspaceSshDialogProps {
  open: boolean;
  onClose: () => void;
  onSubmit: (payload: AddSshGatewayWorkspaceRequest) => Promise<void>;
}

interface WorkspaceSshFormState {
  name: string;
  host: string;
  port: string;
  username: string;
  privateKeyPath: string;
  remoteWorkspacePath: string;
  remoteBackendHost: string;
  remoteBackendPort: string;
}

const INITIAL_FORM: WorkspaceSshFormState = {
  name: "",
  host: "127.0.0.1",
  port: "22",
  username: "",
  privateKeyPath: "",
  remoteWorkspacePath: "",
  remoteBackendHost: "127.0.0.1",
  remoteBackendPort: "8010",
};

function parsePort(value: string, label: string): number {
  const port = Number.parseInt(value, 10);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`${label} 必须是 1-65535 的整数`);
  }
  return port;
}

function required(value: string, label: string): string {
  const trimmed = value.trim();
  if (!trimmed) {
    throw new Error(`${label} 不能为空`);
  }
  return trimmed;
}

export default function WorkspaceSshDialog({
  open,
  onClose,
  onSubmit,
}: WorkspaceSshDialogProps) {
  const [form, setForm] = useState<WorkspaceSshFormState>(INITIAL_FORM);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (open) {
      setError(null);
      setSubmitting(false);
    }
  }, [open]);

  if (!open) {
    return null;
  }

  const update = (key: keyof WorkspaceSshFormState, value: string) => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  const handleSubmit = async () => {
    setError(null);
    let payload: AddSshGatewayWorkspaceRequest;
    try {
      payload = {
        name: form.name.trim() || null,
        host: required(form.host, "SSH 主机"),
        port: parsePort(form.port, "SSH 端口"),
        username: required(form.username, "SSH 用户名"),
        private_key_path: required(form.privateKeyPath, "本机私钥路径"),
        remote_workspace_path: required(form.remoteWorkspacePath, "远程工作区路径"),
        remote_backend_host: required(form.remoteBackendHost, "远程后端监听地址"),
        remote_backend_port: parsePort(form.remoteBackendPort, "远程后端端口"),
      };
    } catch (validationError) {
      setError(validationError instanceof Error ? validationError.message : String(validationError));
      return;
    }

    setSubmitting(true);
    try {
      await onSubmit(payload);
      setForm(INITIAL_FORM);
      onClose();
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : String(submitError));
    } finally {
      setSubmitting(false);
    }
  };

  return createPortal(
    <div className="workspace-dialog-backdrop" role="presentation">
      <section
        className="workspace-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="workspace-ssh-dialog-title"
      >
        <header className="workspace-dialog-header">
          <h2 id="workspace-ssh-dialog-title">添加 SSH 工作区</h2>
          <button
            type="button"
            className="workspace-dialog-icon-button"
            aria-label="关闭"
            onClick={onClose}
            disabled={submitting}
          >
            ×
          </button>
        </header>
        <div className="workspace-dialog-grid">
          <label>
            <span>工作区名称</span>
            <input
              value={form.name}
              onChange={(event) => update("name", event.target.value)}
              placeholder="可留空"
            />
          </label>
          <label>
            <span>SSH 主机</span>
            <input
              value={form.host}
              onChange={(event) => update("host", event.target.value)}
            />
          </label>
          <label>
            <span>SSH 端口</span>
            <input
              value={form.port}
              inputMode="numeric"
              onChange={(event) => update("port", event.target.value)}
            />
          </label>
          <label>
            <span>SSH 用户名</span>
            <input
              value={form.username}
              onChange={(event) => update("username", event.target.value)}
            />
          </label>
          <label className="workspace-dialog-wide">
            <span>本机私钥路径</span>
            <input
              value={form.privateKeyPath}
              onChange={(event) => update("privateKeyPath", event.target.value)}
            />
          </label>
          <label className="workspace-dialog-wide">
            <span>远程工作区路径</span>
            <input
              value={form.remoteWorkspacePath}
              onChange={(event) => update("remoteWorkspacePath", event.target.value)}
            />
          </label>
          <label>
            <span>远程后端监听地址</span>
            <input
              value={form.remoteBackendHost}
              onChange={(event) => update("remoteBackendHost", event.target.value)}
            />
          </label>
          <label>
            <span>远程后端端口</span>
            <input
              value={form.remoteBackendPort}
              inputMode="numeric"
              onChange={(event) => update("remoteBackendPort", event.target.value)}
            />
          </label>
        </div>
        {error ? <div className="workspace-dialog-error">{error}</div> : null}
        <footer className="workspace-dialog-actions">
          <button type="button" onClick={onClose} disabled={submitting}>
            取消
          </button>
          <button
            type="button"
            className="workspace-dialog-primary"
            onClick={handleSubmit}
            disabled={submitting}
          >
            {submitting ? "添加中" : "添加"}
          </button>
        </footer>
      </section>
    </div>,
    document.body,
  );
}
