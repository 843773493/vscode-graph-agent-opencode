export const MAX_RENDERED_ELEMENT_COUNT = 2_000;

export function renderDebugElementTree(container, rootNode, documentRef = document) {
  if (!rootNode) {
    container.innerHTML = '<span class="debug-empty">暂无 DOM 数据</span>';
    return;
  }

  const fragment = documentRef.createDocumentFragment();
  const nodesToRender = [{ node: rootNode, depth: 0 }];
  let renderedNodeCount = 0;
  while (nodesToRender.length > 0 && renderedNodeCount < MAX_RENDERED_ELEMENT_COUNT) {
    const { node, depth } = nodesToRender.pop();
    const row = documentRef.createElement("button");
    row.type = "button";
    row.className = "debug-element-node";
    row.style.paddingLeft = `${10 + depth * 16}px`;
    row.title = node.node_id || "DOM 节点";
    const tag = documentRef.createElement("span");
    tag.className = "debug-element-tag";
    tag.textContent = `<${node.tag || "element"}`;
    row.append(tag);
    for (const [name, value] of Object.entries(node.attributes || {})) {
      const attribute = documentRef.createElement("span");
      attribute.className = "debug-element-attribute";
      attribute.textContent = ` ${name}="${value}"`;
      row.append(attribute);
    }
    const end = documentRef.createElement("span");
    end.className = "debug-element-tag";
    end.textContent = ">";
    row.append(end);
    if (node.text) {
      const text = documentRef.createElement("span");
      text.className = "debug-element-text";
      text.textContent = ` ${node.text}`;
      row.append(text);
    }
    fragment.append(row);
    renderedNodeCount += 1;

    const children = node.children || [];
    for (let index = children.length - 1; index >= 0; index -= 1) {
      nodesToRender.push({ node: children[index], depth: depth + 1 });
    }
  }
  if (nodesToRender.length > 0) {
    const truncationNote = documentRef.createElement("div");
    truncationNote.className = "debug-element-truncated";
    truncationNote.textContent = `DOM 节点过多，仅显示前 ${MAX_RENDERED_ELEMENT_COUNT.toLocaleString("zh-CN")} 个节点`;
    fragment.append(truncationNote);
  }
  container.replaceChildren(fragment);
}
