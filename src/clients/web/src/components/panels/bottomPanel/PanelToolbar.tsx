import type { ReactNode } from 'react';

/** 产品只公布动作及当前值，外层工作台决定标题栏、菜单和紧凑布局。 */
export type PanelToolbarControl = {
  id: string;
  label: string;
  icon?: string;
  disabled?: boolean;
  primary?: boolean;
} & (
  | { kind: 'action'; run: () => void | Promise<unknown>; alternatives?: readonly { label: string; run: () => void | Promise<unknown> }[] }
  | { kind: 'select'; value: string; options: readonly { value: string; label: string }[]; onChange: (value: string) => void }
);
export type RenderPanelToolbar = (controls: readonly PanelToolbarControl[]) => ReactNode;

/** 独立产品测试使用同一动作合同；完整工作台由原生 ActionBar 呈现。 */
export function PanelToolbar({ controls }: { controls: readonly PanelToolbarControl[] }) {
  return <header>{controls.map(control => control.kind === 'action'
    ? <button key={control.id} disabled={control.disabled} aria-label={control.label} onClick={() => void control.run()}>{control.label}</button>
    : control.kind === 'select' ? <select key={control.id} disabled={control.disabled} aria-label={control.label}
      value={control.value} onChange={event => control.onChange(event.target.value)}>
      {control.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
    </select> : null)}</header>;
}
