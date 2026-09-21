/**
 * 安装一次性的指针拖拽生命周期：进入拖拽态、监听 window 的
 * pointermove / pointerup / pointercancel，并返回可提前结束拖拽的清理函数。
 */
export function installPointerDrag(
  className: string,
  onMove: (event: PointerEvent) => void,
  onFinish: () => void,
): () => void {
  const finish = () => {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", finish);
    window.removeEventListener("pointercancel", finish);
    document.body.classList.remove(className);
    onFinish();
  };
  document.body.classList.add(className);
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", finish);
  window.addEventListener("pointercancel", finish);
  return finish;
}
