"""网页正文提取依赖的懒加载边界，不请求外部服务。"""

from __future__ import annotations


def extract_readable_document(
    source: str | bytes, url: str, encoding: str | None = None,
) -> tuple[str | None, str]:
    """本地识别主正文并转为 Markdown，保留段落、链接、列表与代码。"""
    from lxml.html import HTMLParser, document_fromstring
    from markdownify import markdownify
    from readability import Document
    from readability.encoding import get_encoding

    if isinstance(source, bytes):
        # HTTP charset 优先；缺失时让现有解析库识别页首声明，避免全篇编码探测。
        source = source.decode(encoding or get_encoding(source[:4096]), errors="replace")
    if not source.strip():
        raise ValueError(f"网页未提取到可读正文: url={url}")
    # 统一解码后显式按 UTF-8 解析，兼容 XHTML 的 XML 声明，避免重复解释 meta 编码。
    tree = document_fromstring(source.encode("utf-8"), parser=HTMLParser(encoding="utf-8"))
    # 短正文可能让 Readability 保留整个 body；先移除明确的非正文与隐藏区域。
    for element in tree.xpath(
        "//nav | //footer | //aside | //script | //style | //noscript | //template | //svg"
        " | //*[@hidden] | //*[@aria-hidden='true']"
        " | //*[@role='navigation' or @role='banner' or @role='contentinfo']"
    ):
        element.drop_tree()
    # 中文步骤和短列表也属于正文，不能使用面向长文章的默认最短文本阈值。
    document = Document(tree, url=url, min_text_length=1)
    title = document.short_title()
    content = markdownify(document.summary(html_partial=True), heading_style="ATX").strip()
    if not content:
        raise ValueError(f"网页未提取到可读正文: url={url}")
    return (title if title and title != "[no-title]" else None), content
