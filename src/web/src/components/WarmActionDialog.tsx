import { useEffect, useId, useState } from "react";
import { createPortal } from "react-dom";

interface WarmActionDialogProps {
  open: boolean;
  title: string;
  description?: string;
  inputLabel?: string;
  initialValue?: string;
  confirmText: string;
  danger?: boolean;
  onClose: () => void;
  onConfirm: (value: string) => Promise<void>;
}

export default function WarmActionDialog({
  open,
  title,
  description,
  inputLabel,
  initialValue = "",
  confirmText,
  danger = false,
  onClose,
  onConfirm,
}: WarmActionDialogProps) {
  const [value, setValue] = useState(initialValue);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const titleId = useId();

  useEffect(() => {
    if (!open) {
      return;
    }
    setValue(initialValue);
    setSubmitting(false);
    setError(null);
  }, [initialValue, open]);

  useEffect(() => {
    if (!open) {
      return;
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !submitting) {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose, open, submitting]);

  if (!open) {
    return null;
  }

  const normalizedValue = value.trim();
  const submit = async () => {
    if (inputLabel && !normalizedValue) {
      setError(`${inputLabel}不能为空`);
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onConfirm(normalizedValue);
      onClose();
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : String(submitError));
    } finally {
      setSubmitting(false);
    }
  };

  return createPortal(
    <div
      className="workspace-dialog-backdrop"
      role="presentation"
      onPointerDown={() => {
        if (!submitting) {
          onClose();
        }
      }}
    >
      <form
        className="workspace-dialog workspace-action-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onPointerDown={(event) => event.stopPropagation()}
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <header className="workspace-dialog-header">
          <h2 id={titleId}>{title}</h2>
          <button
            type="button"
            className="workspace-dialog-icon-button"
            aria-label="关闭"
            disabled={submitting}
            onClick={onClose}
          >
            ×
          </button>
        </header>
        {description ? <p className="workspace-action-dialog-description">{description}</p> : null}
        {inputLabel ? (
          <div className="workspace-dialog-grid">
            <label className="workspace-dialog-wide">
              <span>{inputLabel}</span>
              <input
                autoFocus
                value={value}
                disabled={submitting}
                maxLength={200}
                onChange={(event) => {
                  setValue(event.target.value);
                  setError(null);
                }}
              />
            </label>
          </div>
        ) : null}
        {error ? <div className="workspace-dialog-error" role="alert">{error}</div> : null}
        <footer className="workspace-dialog-actions">
          <button type="button" disabled={submitting} onClick={onClose}>取消</button>
          <button
            type="submit"
            className={danger ? "workspace-dialog-danger" : "workspace-dialog-primary"}
            disabled={submitting || Boolean(inputLabel && !normalizedValue)}
          >
            {submitting ? "处理中" : confirmText}
          </button>
        </footer>
      </form>
    </div>,
    document.body,
  );
}
