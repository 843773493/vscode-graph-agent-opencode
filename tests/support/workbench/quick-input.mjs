/** 在原滚动条上用真实滚轮移动虚拟列表，保留活动候选和键盘焦点。 */
export async function scrollQuickInput(page, quick, end) {
  const list = quick.getByRole('listbox');
  const track = await list.locator('.scrollbar.vertical').boundingBox();
  if (!track) throw new Error('快速访问滚动条尚未显示，无法滚动状态');
  await page.mouse.move(track.x + track.width / 2, track.y + track.height / 2);
  // 上游会把单次 wheel 归一为有限步长；多类别列表不能靠一次大 delta 到底。
  for (let step = 0; step < 200; step++) {
    const thumb = await list.locator('.scrollbar.vertical .slider').boundingBox();
    if (!thumb || (end ? track.y + track.height - thumb.y - thumb.height : thumb.y - track.y) < 1) return;
    await page.mouse.wheel(0, end ? 1000 : -1000);
    await page.evaluate(() => new Promise(requestAnimationFrame));
  }
  throw new Error(`快速访问滚动未到达${end ? '末尾' : '开头'}`);
}
