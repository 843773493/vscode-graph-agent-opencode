import { afterEach, expect, spyOn, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import * as api from '../../../api/persistentTasks';
import PersistentTasks from './PersistentTasks';
import WarmConfirmProvider from '../../shell/WarmConfirmProvider';

let renderer: ReactTestRenderer | undefined;
const restores: Array<() => void> = [];
afterEach(() => { act(() => renderer?.unmount()); restores.splice(0).forEach(restore => restore()); });

test('任务编辑首次保留原版本，冲突重读后保留草稿并允许用户用最新版本重试', async () => {
  const initial: api.PersistentTaskDTO = {
    task_id: 'task-edit', workspace_id: 'backend', revision: 1, definition_revision: 1,
    application_type: 'automation', application_version: '1', lifecycle: 'active', phase: 'waiting_trigger',
    recovering: false, created_at: '2026-10-29T00:00:00Z', updated_at: '2026-10-29T00:00:00Z',
    configuration: {
      name: '编辑目标', trigger: { type_id: 'builtin.manual', version: '1', timezone: 'UTC' },
      condition: { type_id: 'builtin.always', version: '1' },
      context: { type_id: 'builtin.literal', version: '1', content: '初始正文' },
      action: { type_id: 'builtin.runtime_source', version: '1', target_uri: 'boxteam://workspace/backend/resources/session/session/threads/thread', delivery_mode: 'context_only', delivery_policy: 'after_turn' },
      policies: { completion: 'continuous', misfire: 'skip', match_mode: 'each_match' },
    },
  };
  let records = [initial];
  let publish: ((value: api.PersistentTaskSnapshot) => void) | undefined;
  const snapshot = (): api.PersistentTaskSnapshot => ({ items: records, runtime: { state: 'running', recovering_tasks: 0 } });
  const watch = spyOn(api, 'watchPersistentTasks').mockImplementation(async (_port, _target, signal, listener) => {
    publish = listener; listener(snapshot());
    await new Promise<void>(resolve => signal.addEventListener('abort', () => resolve(), { once: true }));
  });
  const list = spyOn(api, 'listPersistentTasks').mockImplementation(async () => snapshot());
  const history = spyOn(api, 'listPersistentTaskRuns').mockResolvedValue([]);
  const versions: number[] = [];
  const update = spyOn(api, 'updatePersistentTask').mockImplementation(async (_port, _target, task, configuration) => {
    versions.push(task.revision);
    if (task.revision !== records[0].revision) throw new Error('持久任务 revision 已变化');
    records = [{ ...records[0], revision: task.revision + 1, configuration }];
    return records[0];
  });
  restores.push(() => watch.mockRestore(), () => list.mockRestore(), () => history.mockRestore(), () => update.mockRestore());
  await act(async () => {
    renderer = create(<WarmConfirmProvider><PersistentTasks apiPort={0} target={{ gateway_id: "gateway-test", workspace_id: 'backend' }}
      onAction={(action, source, configuration) => source.run(action, configuration)} sessionId="session" goalSessionId="session" onOpenSession={async () => {}} onOpenThread={async () => {}} onStatusChange={() => {}} /></WarmConfirmProvider>);
  });
  const root = () => renderer!.root;
  const button = (text: string) => root().findAllByType('button').find(node => node.children.includes(text))!;
  await act(async () => { root().findAllByType('button').find(node => node.props['aria-pressed'] === false)!.props.onClick(); });
  await act(async () => { button('编辑任务').props.onClick(); });
  await act(async () => { root().findByProps({ 'aria-label': '提示内容' }).props.onChange({ target: { value: '用户未保存草稿' } }); });
  // 在编辑期间发布新版本：首次提交仍必须冲突，不能用 SSE 的新 revision 静默覆盖。
  records = [{ ...initial, revision: 2, configuration: { ...initial.configuration!, context: { ...initial.configuration!.context!, content: '远端修改' } } }];
  await act(async () => { publish?.(snapshot()); });
  await act(async () => { root().findByType('form').props.onSubmit({ preventDefault() {} }); });
  expect(versions).toEqual([1]);
  expect(root().findByProps({ 'aria-label': '提示内容' }).props.value).toBe('用户未保存草稿');
  expect(root().findAllByProps({ role: 'alert' }).some(node => node.children.join('').includes('revision'))).toBe(true);
  await act(async () => { root().findByType('form').props.onSubmit({ preventDefault() {} }); });
  expect(versions).toEqual([1, 2]);
  expect(records[0].configuration?.context?.content).toBe('用户未保存草稿');
  expect(root().findAllByType('form')).toHaveLength(0);
});
