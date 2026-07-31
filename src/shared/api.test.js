import { afterEach, describe, expect, test } from 'bun:test';

import {
  streamJobEvents,
  streamSessionEvents,
} from './api.js';

const originalFetch = globalThis.fetch;

function streamResponse(block) {
  return new Response(block, {
    headers: { 'content-type': 'text/event-stream; charset=utf-8' },
  });
}

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe('共享 SSE API', () => {
  test('Trace DTO 经过生成校验并从 raw 提取展示载荷', async () => {
    const trace = {
      event_id: 'trace-1',
      session_id: 'session-1',
      job_id: 'job-1',
      type: 'goal_updated',
      phase: 'goal',
      title: 'Goal 更新',
      content: 'Goal 更新',
      timestamp: '2026-07-28T00:00:00Z',
      raw: { payload: { goal: { goal_id: 'goal-1' } } },
    };
    globalThis.fetch = async () => streamResponse(
      `id: trace-1\nevent: trace\ndata: ${JSON.stringify(trace)}\n\n`,
    );
    const events = [];

    await streamSessionEvents(8010, 'session-1', {
      onEvent: (event) => events.push(event),
    });

    expect(events).toHaveLength(1);
    expect(events[0].event).toEqual(trace);
    expect(events[0].payload).toEqual({ goal: { goal_id: 'goal-1' } });
  });

  test('Job envelope 使用规范化事件名并保留 raw_type', async () => {
    const envelope = {
      event: {
        event_id: 'job-event-1',
        session_id: 'session-1',
        job_id: 'job-1',
        type: 'session.completed',
        time: '2026-07-28T00:00:00Z',
        payload: {
          job_id: 'job-1',
          status: 'completed',
          progress: 100,
        },
      },
      raw_type: 'job_completed',
      raw_payload: {},
    };
    globalThis.fetch = async () => streamResponse(
      `id: job-event-1\nevent: session.completed\ndata: ${JSON.stringify(envelope)}\n\n`,
    );
    const events = [];

    await streamJobEvents(8010, 'job-1', {
      onEvent: (event) => events.push(event),
    });

    expect(events[0].eventType).toBe('session.completed');
    expect(events[0].rawType).toBe('job_completed');
    expect(events[0].payload.status).toBe('completed');
  });

  test('Job 重连通过 Last-Event-ID 续传', async () => {
    let requestHeaders;
    globalThis.fetch = async (_url, options) => {
      requestHeaders = options.headers;
      return streamResponse(': ping\n\n');
    };

    await streamJobEvents(8010, 'job-1', {
      afterEventId: 'job-event-previous',
    });

    expect(requestHeaders['Last-Event-ID']).toBe('job-event-previous');
  });

  test('非法 JSON 不再静默降级为文本', async () => {
    globalThis.fetch = async () => streamResponse(
      'id: trace-1\nevent: trace\ndata: {broken}\n\n',
    );

    await expect(streamSessionEvents(8010, 'session-1'))
      .rejects.toThrow('SSE JSON 无法解析');
  });
});
