import React, { useRef, useEffect, useState, useCallback } from 'react';
import { useAppState } from '../hooks';
import { postDebug } from '../vscode';

export default function Composer() {
  const { state, sendMessage, toggleExpandDetails } = useAppState();
  const [input, setInput] = React.useState('');
  const isGenerating = state.activeJob?.status === 'running';
  const hasContent = input.trim().length > 0;

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
      const value = input;
      setInput(value.slice(0, start) + '\n' + value.slice(end));
      return;
    }
    e.preventDefault();
    handleSend();
  };

  useEffect(() => {
    const sendBtn = document.getElementById('sendButton') as HTMLButtonElement | null;
    const clearBtn = document.getElementById('clearInputButton') as HTMLButtonElement | null;
    if (sendBtn) sendBtn.disabled = !hasContent || isGenerating;
    if (clearBtn) clearBtn.classList.toggle('hidden', !hasContent);
  }, [hasContent, isGenerating]);

  return (
    <footer className="composer">
      <div className="composer-copy">
        <textarea
          id="input"
          placeholder="输入消息后回车发送，Ctrl+Enter 换行"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={isGenerating}
          rows={1}
        />
        <div className="composer-hint">Enter 发送 · Ctrl+Enter 插入换行</div>
      </div>
      <div className="composer-actions">
        <div className="composer-actions-left">
          <button id="attachButton" type="button" title="添加附件" onClick={() => postDebug('TODO: attach')} />
          <button id="mentionButton" type="button" title="@提及" onClick={() => postDebug('TODO: mention')} />
        </div>
        <div className="composer-actions-right">
          <button id="agentSelectButton" type="button" title="选择Agent">
            <span>{state.currentSession?.agent_id || 'default'}</span>
            <svg viewBox="0 0 16 16"><path d="M8 1L9 4H12L9.5 6L10.5 9L8 7L5.5 9L6.5 6L4 4H7L8 1ZM4 10L5 13H8L6.5 15L7.5 12H10.5L9.5 15H12.5L11.5 12H14.5L13 10H4z"/></svg>
          </button>
          <button id="clearInputButton" type="button" title="清空输入" className="hover-only" onClick={() => setInput('')} />
          <button id="sendButton" type="button" className="send-button" disabled={!hasContent || isGenerating} onClick={handleSend}>
            <svg viewBox="0 0 16 16"><path d="M1.5 1.5L14.5 8L1.5 14.5V9L10 8L1.5 7V1.5Z" /></svg>
          </button>
        </div>
      </div>
    </footer>
  );
}
