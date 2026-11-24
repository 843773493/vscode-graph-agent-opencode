import { afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { ConfigurationProxyFields } from './ConfigurationProxyFields.js';

let view: ReactTestRenderer;
afterEach(() => act(() => view?.unmount()));

test('模式切换写互斥完整块，恢复继承仅删除本地定义', () => {
  const change = mock();
  act(() => { view = create(<ConfigurationProxyFields local={{ mode: 'explicit', url: 'http://proxy.test',
    auth: { username: 'name', password: 'secret:sec_test' } }} disabled={false} onChange={change} />); });
  const mode = view.root.findByType('select');
  act(() => mode.props.onChange({ target: { value: 'direct' } }));
  act(() => mode.props.onChange({ target: { value: '' } }));
  expect(change.mock.calls).toEqual([[{ mode: 'direct' }], [undefined]]);
});

test('编辑继承代理字段生成完整本地块；无父根不提供恢复继承', () => {
  const change = mock(), inherited = { mode: 'explicit', url: 'http://parent.test', bypass: ['localhost'] };
  act(() => { view = create(<ConfigurationProxyFields inherited={inherited} disabled={false} allowInherit={false} onChange={change} />); });
  const url = view.root.find(node => node.props['data-proxy-field'] === 'url');
  act(() => url.props.onChange({ target: { value: 'http://child.test' } }));
  expect(change.mock.calls).toEqual([[{ ...inherited, url: 'http://child.test' }]]);
  expect(inherited.url).toBe('http://parent.test');
  expect(view.root.findAllByType('option').map(node => node.props.value)).not.toContain('');
});

test('无本地代理时继承仅为当前状态，设值后才允许恢复继承', () => {
  const change = mock();
  act(() => { view = create(<ConfigurationProxyFields inherited={{ mode: 'direct' }} disabled={false} onChange={change} />); });
  expect(view.root.findByProps({ value: '', disabled: true }).type).toBe('option');
  act(() => { view.update(<ConfigurationProxyFields local={{ mode: 'direct' }} inherited={{ mode: 'direct' }} disabled={false} onChange={change} />); });
  expect(view.root.findByProps({ value: '', disabled: false }).type).toBe('option');
  act(() => view.root.findByType('select').props.onChange({ target: { value: '' } }));
  expect(change.mock.calls).toEqual([[undefined]]);
});
