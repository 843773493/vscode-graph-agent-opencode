/**
 * 端口字面量校验的唯一实现。
 *
 * 端口必须是十进制整数；`Number()` 会把 "1e3"、"0x50"、"0b101"、"12.0" 静默
 * 收敛成完全不同的数字（"0x50" 变 80），把非法输入当成合法端口提交，SSH 会连到
 * 用户没填的端口上。端口转发面板与 SSH 连接表单此前各写一份口径不同的实现，
 * 这里收敛为唯一一份。
 */
export const DECIMAL_PORT_PATTERN = /^\d{1,5}$/;

export function isValidPortLiteral(value: string): boolean {
  return DECIMAL_PORT_PATTERN.test(value) && Number(value) >= 1 && Number(value) <= 65535;
}

export function parsePortLiteral(value: string, label: string): number {
  if (!isValidPortLiteral(value)) {
    throw new Error(`${label}必须是 1–65535 之间的十进制整数`);
  }
  return Number(value);
}
