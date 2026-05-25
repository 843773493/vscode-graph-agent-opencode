import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useAppState } from '../hooks';

function resizeTextarea(textarea: HTMLTextAreaElement | null) {
  if (!textarea) return;
  textarea.style.height = '0px';
  textarea.style.height = `${Math.min(textarea.scrollHeight, 220)}px`;
}

export default function Composer() {
  const { state, sendMessage } = useAppState();
  const [input, setInput] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const isGenerating = state.activeJob?.status === 'running';
  const hasContent = input.trim().length > 0;
  const currentAgent = state.currentSession?.agent_id || 'default';
  const composerHint = useMemo(() => {
    if (isGenerating) return '当前任务正在运行，等待完成后再发送新消息';
    return 'Enter 发送 · Ctrl+Enter 换行';
  }, [isGenerating]);

  useEffect(() => {
    resizeTextarea(textareaRef.current);
  }, [input]);

  const handleSend = useCallback(() => {
    const content = input.trim();
    if (!content || isGenerating) return;
    setInput('');
    sendMessage(content);
  }, [input, isGenerating, sendMessage]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Enter') return;
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      const start = e.currentTarget.selectionStart ?? input.length;
      const end = e.currentTarget.selectionEnd ?? input.length;
      setInput(input.slice(0, start) + '\n' + input.slice(end));
      return;
    }
    e.preventDefault();
    handleSend();
  };

  return (
    <footer className="composer">
      <div className="composer-context-row" aria-label="输入上下文">
        <span className="context-chip">@workspace</span>
        <span className="context-chip">#current-file</span>
        <span className="context-chip">{currentAgent}</span>
      </div>
      <div className="composer-surface">
        <div className="composer-copy">
          <textarea
            ref={textareaRef}
            id="input"
            placeholder="输入消息后回车发送，Ctrl+Enter 换行"
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={isGenerating}
            rows={1}
          />
          <div className="composer-hint">{composerHint}</div>
        </div>
        <div className="composer-actions">
          <div className="composer-actions-left">
            <button id="attachButton" type="button" title="添加附件" disabled>
              附件
            </button>
            <button id="mentionButton" type="button" title="@提及" disabled>
              提及
            </button>
          </div>
          <div className="composer-actions-right">
            <button id="agentSelectButton" type="button" title="选择Agent">
              <span>{currentAgent}</span>
              <svg viewBox="0 0 16 16"><path d="M8 1L9 4H12L9.5 6L10.5 9L8 7L5.5 9L6.5 6L4 4H7L8 1ZM4 10L5 13H8L6.5 15L7.5 12H10.5L9.5 15H12.5L11.5 12H14.5L13 10H4z"/></svg>
            </button>
            {hasContent && (
              <button id="clearInputButton" type="button" title="清空输入" className="hover-only" onClick={() => setInput('')}>
                清空
              </button>
            )}
            <button id="sendButton" type="button" className="send-button" disabled={!hasContent || isGenerating} onClick={handleSend}>
              <svg viewBox="0 0 16 16"><path d="M1.5 1.5L14.5 8L1.5 14.5V9L10 8L1.5 7V1.5Z" /></svg>
              <span>发送</span>
            </button>
          </div>
        </div>
      </div>
    </footer>
  );
}