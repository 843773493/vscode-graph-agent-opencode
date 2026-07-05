import React, { useEffect, useId, useRef, useState } from 'react';

interface SessionNameDialogProps {
  open: boolean;
  title: string;
  label: string;
  initialValue: string;
  confirmText: string;
  submitting: boolean;
  error: string | null;
  onCancel: () => void;
  onSubmit: (title: string) => void;
}

export default function SessionNameDialog({
  open,
  title,
  label,
  initialValue,
  confirmText,
  submitting,
  error,
  onCancel,
  onSubmit,
}: SessionNameDialogProps): React.ReactNode {
  const [draft, setDraft] = useState(initialValue);
  const [localError, setLocalError] = useState('');
  const inputRef = useRef<HTMLInputElement | null>(null);
  const inputId = useId();

  useEffect(() => {
    if (!open) {
      return;
    }

    setDraft(initialValue);
    setLocalError('');
    window.setTimeout(() => {
      inputRef.current?.focus();
      inputRef.current?.select();
    }, 0);
  }, [initialValue, open]);

  useEffect(() => {
    if (!open) {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !submitting) {
        onCancel();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onCancel, open, submitting]);

  if (!open) {
    return null;
  }

  const message = localError || error;
  const normalizedDraft = draft.trim();
  const confirmDisabled = submitting || !normalizedDraft;
  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!normalizedDraft) {
      setLocalError('会话名称不能为空');
      return;
    }
    setLocalError('');
    onSubmit(normalizedDraft);
  };

  return (
    <div
      className="session-name-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby={`${inputId}-title`}
      onPointerDown={() => {
        if (!submitting) {
          onCancel();
        }
      }}
    >
      <form
        className="session-name-dialog"
        onSubmit={handleSubmit}
        onPointerDown={(event) => event.stopPropagation()}
      >
        <div className="session-name-header">
          <h2 id={`${inputId}-title`}>{title}</h2>
          <button
            type="button"
            className="session-name-close"
            aria-label="关闭"
            disabled={submitting}
            onClick={onCancel}
          >
            ×
          </button>
        </div>
        <label className="session-name-field" htmlFor={inputId}>
          <span>{label}</span>
          <input
            ref={inputRef}
            id={inputId}
            value={draft}
            disabled={submitting}
            maxLength={120}
            required
            onChange={(event) => {
              setDraft(event.target.value);
              setLocalError('');
            }}
          />
        </label>
        {message ? <div className="session-name-error">{message}</div> : null}
        <div className="session-name-actions">
          <button type="button" disabled={submitting} onClick={onCancel}>
            取消
          </button>
          <button type="submit" className="session-name-confirm" disabled={confirmDisabled}>
            {submitting ? '保存中' : confirmText}
          </button>
        </div>
      </form>
    </div>
  );
}
