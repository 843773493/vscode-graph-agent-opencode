"""网页提取只运行本地解析器，保留主正文的阅读结构。"""

from __future__ import annotations

import pytest

from app.runtime.web_content import extract_readable_document


def test_extracts_article_markdown_without_navigation_or_footer() -> None:
    title, text = extract_readable_document("""
        <html><head><title>安装指南</title></head><body>
        <nav><a href="/ads">广告与导航</a></nav>
        <article><h1>安装指南</h1>
        <p>这是项目的安装说明，介绍如何准备环境、安装依赖和启动应用。</p>
        <p>请先阅读<a href="/docs">完整文档</a>，然后依次执行下面的步骤。</p>
        <ul><li>准备环境</li><li>安装依赖</li></ul>
        <pre><code>if ready:\n    run()</code></pre>
        <p>完成上述步骤后，检查服务是否正常运行，并继续阅读项目说明。</p>
        </article><footer>版权与推广</footer><script>hidden_script()</script>
        </body></html>
    """, "https://example.com/guide")
    assert title == "安装指南"
    assert "安装说明" in text
    assert "广告与导航" not in text
    assert "版权与推广" not in text
    assert "hidden_script" not in text
    assert "[完整文档](https://example.com/docs)" in text
    assert "- 准备环境" in text or "* 准备环境" in text
    assert "```" in text
    assert "if ready:\n    run()" in text


def test_extracts_short_page_without_inventing_title() -> None:
    title, text = extract_readable_document("<p>简单的页面正文。</p>", "https://example.com")
    assert title is None
    assert "简单的页面正文。" in text


@pytest.mark.parametrize("source", ["<html><body></body></html>", "", " \n ", b""])
def test_empty_page_reports_extraction_failure(source: str | bytes) -> None:
    with pytest.raises(ValueError, match="未提取到可读正文"):
        extract_readable_document(source, "https://example.com/empty")


@pytest.mark.parametrize("as_bytes", [False, True])
def test_extracts_xhtml_with_xml_encoding_declaration(as_bytes: bool) -> None:
    source = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>安装指南</title></head>'
        '<body><article><p>这是 XHTML 格式的中文正文。</p></article></body></html>'
    )
    title, text = extract_readable_document(
        source.encode() if as_bytes else source, "https://example.com/docs",
    )
    assert title == "安装指南"
    assert "这是 XHTML 格式的中文正文。" in text
