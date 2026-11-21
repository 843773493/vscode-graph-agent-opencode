import { expect, test } from 'bun:test';
import { parsePortLiteral } from './portLiteral';

test('端口必须是范围内十进制整数字面量', () => {
  for (const value of ['1e3', '0x50', '0b101', '12.0', '-1', '0', '65536', ' 80', '']) {
    expect(() => parsePortLiteral(value, '监听端口')).toThrow();
  }
  expect(parsePortLiteral('1', '监听端口')).toBe(1);
  expect(parsePortLiteral('65535', '服务端口')).toBe(65535);
});
