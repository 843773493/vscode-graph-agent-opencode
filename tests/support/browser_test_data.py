from __future__ import annotations

from urllib.parse import quote


def browser_test_data_url() -> str:
    """小型页面数据，用于远端 Gateway Browser 资源验收。"""
    html = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Gateway Docker Browser</title></head>
<body><h1>Gateway Docker Browser Resource</h1><button id="ok">OK</button></body>
</html>"""
    return "data:text/html;charset=utf-8," + quote(html)
