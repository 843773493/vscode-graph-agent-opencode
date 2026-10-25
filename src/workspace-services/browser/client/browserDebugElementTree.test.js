import { describe, expect, test } from "bun:test";

import {
  MAX_RENDERED_ELEMENT_COUNT,
  renderDebugElementTree,
} from "./browserDebugElementTree.js";

function createElement(tagName) {
  return {
    tagName,
    children: [],
    style: {},
    append(...children) {
      this.children.push(...children);
    },
  };
}

function createDocument() {
  return {
    createElement,
    createDocumentFragment() {
      return {
        children: [],
        append(child) {
          this.children.push(child);
        },
      };
    },
  };
}

function createContainer() {
  return {
    innerHTML: "",
    children: [],
    replacements: 0,
    replaceChildren(...children) {
      this.replacements += 1;
      this.children = children.flatMap((child) => child.children || [child]);
    },
  };
}

describe("renderDebugElementTree", () => {
  test("保留前序显示、属性文本和层级缩进，并一次替换可见树", () => {
    const container = createContainer();
    renderDebugElementTree(container, {
      node_id: "root",
      tag: "main",
      attributes: { role: "main" },
      text: "页面",
      children: [{
        node_id: "child",
        tag: "button",
        children: [],
      }],
    }, createDocument());

    expect(container.replacements).toBe(1);
    expect(container.children.map((row) => row.title)).toEqual(["root", "child"]);
    expect(container.children[0].style.paddingLeft).toBe("10px");
    expect(container.children[1].style.paddingLeft).toBe("26px");
    expect(container.children[0].children.map((node) => node.textContent)).toEqual([
      "<main", ' role="main"', ">", " 页面",
    ]);
  });

  test("深树按节点预算迭代渲染并提示截断", () => {
    const depth = 12_000;
    const root = { children: [] };
    let node = root;
    for (let index = 1; index < depth; index += 1) {
      const child = { children: [] };
      node.children.push(child);
      node = child;
    }
    const container = createContainer();

    renderDebugElementTree(container, root, createDocument());

    expect(container.replacements).toBe(1);
    expect(MAX_RENDERED_ELEMENT_COUNT).toBe(2_000);
    expect(container.children).toHaveLength(MAX_RENDERED_ELEMENT_COUNT + 1);
    expect(container.children.at(-2).style.paddingLeft).toBe(
      `${10 + (MAX_RENDERED_ELEMENT_COUNT - 1) * 16}px`,
    );
    expect(container.children.at(-1).className).toBe("debug-element-truncated");
    expect(container.children.at(-1).textContent).toBe("DOM 节点过多，仅显示前 2,000 个节点");
  });

  test("刚好到达节点预算且没有剩余节点时不显示截断提示", () => {
    const container = createContainer();
    const root = {
      children: Array.from(
        { length: MAX_RENDERED_ELEMENT_COUNT - 1 },
        () => ({ children: [] }),
      ),
    };

    renderDebugElementTree(container, root, createDocument());

    expect(container.children).toHaveLength(MAX_RENDERED_ELEMENT_COUNT);
    expect(container.children.at(-1).className).toBe("debug-element-node");
  });

  test("无 DOM 数据时显示空状态", () => {
    const container = createContainer();

    renderDebugElementTree(container, null, createDocument());

    expect(container.innerHTML).toBe('<span class="debug-empty">暂无 DOM 数据</span>');
    expect(container.replacements).toBe(0);
  });
});
