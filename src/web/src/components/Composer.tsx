import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useAppState } from '../hooks';

function resizeTextarea(textarea: HTMLTextAreaElement | null) {
  if (!textarea) {
    return;
  }

  textarea.style.height = '0px';
  textarea.style.height = `${Math.min(textarea.scrollHeight, 220)}px`;
}

function insertLineBreak(value: string, start: number, end: number): string {
  return value.slice(0, start) + '\n' + value.slice(end);
}

export default function Composer() {
  const { state, sendMessage, interruptSession } = useAppState();
  const [input, setInput] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const isGenerating = state.activeJob?.status === 'running';
  const hasContent = input.trim().length > 0;
  const currentAgent = state.currentSession?.agent_id || 'default';
  const showInterrupt = state.currentSession
    ? state.pendingConversations.get(state.currentSession.session_id)?.pending ?? false
    : false;
  const composerHint = useMemo(() => {
    if (showInterrupt) {
      return '正在生成中，点击中断按钮停止生成';
    }
    if (isGenerating) {
      return '当前任务正在运行，等待完成后再发送新消息';
    }
    return 'Enter 发送 · Ctrl+Enter 换行';
  }, [isGenerating, showInterrupt]);

  useEffect(() => {
    resizeTextarea(textareaRef.current);
  }, [input]);

  const handleSend = () => {
    const content = input.trim();
    if (!content || isGenerating) {
      return;
    }

    setInput('');
    void sendMessage(content);
  };

  const handleInterrupt = () => {
    if (!showInterrupt) {
      return;
    }

    void interruptSession();
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Enter') {
      return;
    }

    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      const start = e.currentTarget.selectionStart ?? input.length;
      const end = e.currentTarget.selectionEnd ?? input.length;
      setInput(insertLineBreak(input, start, end));
      return;
    }

    if (e.shiftKey) {
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
            <button id="attachButton" type="button" title="添加附件" disabled={showInterrupt}>
              <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="M6.5 1.5a3.5 3.5 0 0 1 4.95 0l2.05 2.05a4.5 4.5 0 0 1-6.364 6.364l-3.18-3.18a2.5 2.5 0 0 1 3.535-3.535l2.121 2.121" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/></svg>
            </button>
            <button id="mentionButton" type="button" title="提及成员 (@)" disabled={showInterrupt}>
              <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="M8 3a3 3 0 1 0 0 6 4 4 0 1 1 0 8" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/></svg>
            </button>
          </div>
          <div className="composer-actions-right">
            <div className="composer-actions-row">
              <button id="agentSelectButton" type="button" title={`选择Agent: ${currentAgent}`} disabled={showInterrupt}>
                <span className="composer-agent-label">{currentAgent}</span>
                <svg viewBox="0 0 16 16" width="12" height="12"><path d="M8 1L9 4H12L9.5 6L10.5 9L8 7L5.5 9L6.5 6L4 4H7L8 1ZM4 10L5 13H8L6.5 15L7.5 12H10.5L9.5 15H12.5L11.5 12H14.5L13 10H4z"/></svg>
              </button>
              {hasContent && !showInterrupt && (
                <button id="clearInputButton" type="button" title="清空输入" className="hover-only" onClick={() => setInput('')}>
                  <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="M3 3l10 10M13 3L3 13" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></svg>
                </button>
              )}
              {showInterrupt ? (
                <button id="interruptButton" type="button" className="interrupt-button" onClick={handleInterrupt} title="中断生成">
                  <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><rect x="3" y="3" width="10" height="10" rx="2" fill="currentColor"/></svg>
                </button>
              ) : (
                <button id="sendButton" type="button" className="send-button" disabled={!hasContent || isGenerating} onClick={handleSend} title={hasContent ? '发送消息' : '输入消息以启用发送'}>
                  <svg viewBox="0 0 16 16" width="12" height="12"><path d="M1.5 1.5L14.5 8L1.5 14.5V9L10 8L1.5 7V1.5Z" /></svg>
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    </footer>
  );
}
