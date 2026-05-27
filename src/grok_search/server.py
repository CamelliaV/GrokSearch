import sys
import json
from pathlib import Path

# 支持直接运行：添加 src 目录到 Python 路径
src_dir = Path(__file__).parent.parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from fastmcp import FastMCP, Context
from typing import Annotated

try:
    from grok_search.providers.grok import GrokSearchProvider
    from grok_search.logger import log_info
    from grok_search.config import config
except ImportError:
    from .providers.grok import GrokSearchProvider
    from .logger import log_info
    from .config import config

import httpx
import asyncio

mcp = FastMCP("grok-search")

_HTTP_CLIENT: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.AsyncClient(timeout=90.0, follow_redirects=True)
    return _HTTP_CLIENT


@mcp.tool(
    name="web_search",
    output_schema=None,
    description="""
    Performs a deep web search based on the given query and returns Grok's answer directly.

    This tool extracts sources if provided by upstream, caches them, and returns:
    - session_id: string (When you feel confused or curious about the main content, use this field to invoke the get_sources tool to obtain the corresponding list of information sources)
    - content: string (answer only)
    - sources_count: int
    """,
    meta={"version": "2.0.0", "author": "guda.studio"},
)
async def web_search(
    query: Annotated[str, "Clear, self-contained natural-language search query."],
    platform: Annotated[str, "Target platform to focus on (e.g., 'Twitter', 'GitHub', 'Reddit'). Leave empty for general web search."] = "",
    model: Annotated[str, "Optional model ID for this request only. This value is used ONLY when user explicitly provided."] = "",
) -> str:
    try:
        api_url = config.grok_api_url
        api_key = config.grok_api_key
    except ValueError as e:
        return f"配置错误: {str(e)}"

    effective_model = config.grok_model
    if model:
        effective_model = model

    grok_provider = GrokSearchProvider(api_url, api_key, effective_model, _get_http_client())

    try:
        result = await grok_provider.search(query, platform)
    except Exception as e:
        return f"搜索失败: {str(e)}"

    if not result or not result.strip():
        # 业务层重试：请求成功但内容为空
        try:
            result = await grok_provider.search(query, platform)
        except Exception as e:
            return f"搜索失败: {str(e)}"

    if not result or not result.strip():
        return "搜索失败: Grok API 返回空结果，请稍后重试"

    return result


async def _call_tavily_extract(url: str) -> str | None:
    api_url = config.tavily_api_url
    api_key = config.tavily_api_key
    if not api_key:
        return None
    endpoint = f"{api_url.rstrip('/')}/extract"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"urls": [url], "format": "markdown"}
    try:
        client = _get_http_client()
        response = await client.post(endpoint, headers=headers, json=body, timeout=60.0)
        response.raise_for_status()
        data = response.json()
        if data.get("results") and len(data["results"]) > 0:
            content = data["results"][0].get("raw_content", "")
            return content if content and content.strip() else None
        return None
    except Exception:
        return None


async def _call_firecrawl_scrape(url: str, ctx=None) -> str | None:
    api_url = config.firecrawl_api_url
    api_key = config.firecrawl_api_key
    if not api_key:
        return None
    endpoint = f"{api_url.rstrip('/')}/scrape"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    max_retries = config.retry_max_attempts
    client = _get_http_client()
    for attempt in range(max_retries):
        body = {
            "url": url,
            "formats": ["markdown"],
            "timeout": 60000,
            "waitFor": (attempt + 1) * 1500,
        }
        try:
            response = await client.post(endpoint, headers=headers, json=body, timeout=90.0)
            response.raise_for_status()
            data = response.json()
            markdown = data.get("data", {}).get("markdown", "")
            if markdown and markdown.strip():
                return markdown
            await log_info(ctx, f"Firecrawl: markdown为空, 重试 {attempt + 1}/{max_retries}", config.debug_enabled)
        except Exception as e:
            await log_info(ctx, f"Firecrawl error: {e}", config.debug_enabled)
            return None
    return None


@mcp.tool(
    name="web_fetch",
    output_schema=None,
    description="""
    Fetches and extracts complete content from a URL, returning it as a structured Markdown document.

    **Key Features:**
        - **Full Content Extraction:** Retrieves and parses all meaningful content (text, images, links, tables, code blocks).
        - **Markdown Conversion:** Converts HTML structure to well-formatted Markdown with preserved hierarchy.
        - **Content Fidelity:** Maintains 100% content fidelity without summarization or modification.

    **Edge Cases & Best Practices:**
        - Ensure URL is complete and accessible (not behind authentication or paywalls).
        - May not capture dynamically loaded content requiring JavaScript execution.
        - Large pages may take longer to process; consider timeout implications.
    """,
    meta={"version": "1.3.0", "author": "guda.studio"},
)
async def web_fetch(
    url: Annotated[str, "Valid HTTP/HTTPS web address pointing to the target page. Must be complete and accessible."],
    ctx: Context = None
) -> str:
    await log_info(ctx, f"Begin Fetch: {url}", config.debug_enabled)

    result = await _call_tavily_extract(url)
    if result:
        await log_info(ctx, "Fetch Finished (Tavily)!", config.debug_enabled)
        return result

    await log_info(ctx, "Tavily unavailable or failed, trying Firecrawl...", config.debug_enabled)
    result = await _call_firecrawl_scrape(url, ctx)
    if result:
        await log_info(ctx, "Fetch Finished (Firecrawl)!", config.debug_enabled)
        return result

    await log_info(ctx, "Fetch Failed!", config.debug_enabled)
    if not config.tavily_api_key and not config.firecrawl_api_key:
        return "配置错误: TAVILY_API_KEY 和 FIRECRAWL_API_KEY 均未配置"
    return "提取失败: 所有提取服务均未能获取内容"


def main():
    import signal
    import os
    import threading

    # 信号处理（仅主线程）
    if threading.current_thread() is threading.main_thread():
        def handle_shutdown(signum, frame):
            os._exit(0)
        signal.signal(signal.SIGINT, handle_shutdown)
        if sys.platform != 'win32':
            signal.signal(signal.SIGTERM, handle_shutdown)

    # Windows 父进程监控
    if sys.platform == 'win32':
        import time
        import ctypes
        parent_pid = os.getppid()

        def is_parent_alive(pid):
            """Windows 下检查进程是否存活"""
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return True
            exit_code = ctypes.c_ulong()
            result = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return result and exit_code.value == STILL_ACTIVE

        def monitor_parent():
            while True:
                if not is_parent_alive(parent_pid):
                    os._exit(0)
                time.sleep(2)

        threading.Thread(target=monitor_parent, daemon=True).start()

    try:
        mcp.run(transport="stdio", show_banner=False)
    except KeyboardInterrupt:
        pass
    finally:
        if _HTTP_CLIENT and not _HTTP_CLIENT.is_closed:
            asyncio.get_event_loop().run_until_complete(_HTTP_CLIENT.aclose())
        os._exit(0)


if __name__ == "__main__":
    main()
