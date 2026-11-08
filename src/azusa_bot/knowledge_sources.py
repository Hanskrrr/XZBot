"""Read public Moegirl article text without an LLM, login, or API bypass."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import parse_qs, quote, unquote, urlsplit

from bs4 import BeautifulSoup, NavigableString
from bs4.element import TemplateString
import httpx

from .lore import MAX_DOCUMENT_BYTES

MAX_PAGE_BYTES = 3 * 1024 * 1024
LICENSE = "CC BY-NC-SA 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/"
ALLOWED_HOSTS = {"zh.moegirl.org.cn", "mzh.moegirl.org.cn"}


def article_url(value: str) -> str:
    """Canonicalise to one fixed origin; never fetch arbitrary user URLs."""
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("source_url_invalid")
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname not in ALLOWED_HOSTS
            or url.username or url.password or url.port not in {None, 443}):
            raise ValueError()
        query = parse_qs(url.query, strict_parsing=True) if url.query else {}
        if set(query) - {"title", "oldid", "variant"} or any(len(v) != 1 for v in query.values()):
            raise ValueError()
        title = query.get("title", [unquote(url.path.lstrip("/"))])[0]
        if (not 1 <= len(title) <= 200 or title in {"index.php", "api.php"}
            or any(c in title for c in "|#[]{}<>\\") or any(ord(c) < 32 for c in title)
            or title.startswith(("Special:", "特殊:"))):
            raise ValueError()
        oldid = query.get("oldid", [""])[0]
        if oldid and not re.fullmatch(r"[0-9]{1,16}", oldid):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("source_url_requires_moegirl_article") from None
    result = "https://zh.moegirl.org.cn/" + quote(title, safe="/:")
    return result + ("?oldid=" + oldid if oldid else "")


def parse_moegirl(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    page = {}
    metadata = soup.find("script", id="MOE_SKIN_PAGE_DATA", type="application/json")
    if metadata and metadata.string:
        try:
            page = json.loads(metadata.string)
        except ValueError:
            raise ValueError("source_page_format_changed") from None
        if not isinstance(page, dict):
            raise ValueError("source_page_format_changed")
    article = soup.select_one("#mw-content-text > .mw-parser-output")
    if article is None or page.get("isarticle") is False:
        raise ValueError("source_access_restricted")
    # The current public MoeSkin puts normal article HTML in a <template>.
    # BeautifulSoup excludes TemplateString by default; explicitly include it.
    title = page.get("titletext") or page.get("title")
    if not isinstance(title, str) or not 1 <= len(title) <= 200:
        title = soup.title.get_text().split(" - 萌娘百科", 1)[0] if soup.title else "萌娘百科原文"
    copyright_html = str(page.get("copyright", ""))
    license_markup = html + copyright_html
    if not re.search(r"creativecommons\.org/licenses/by-nc-sa/4\.0(?:/|[\"'])", license_markup):
        raise ValueError("source_license_not_recognized")
    for element in article.select("script,style,noscript,nav,iframe,form,.mw-editsection,#toc,.toc,.navbox,.navigation-not-searchable,.sidebar-character"):
        element.decompose()
    # Retain deletion/joke markup rather than flattening a crossed-out claim
    # into plain fact. No generated summary, corrections or wording changes.
    for element in article.select("s,del,strike"):
        element.insert_before(NavigableString("~~"))
        element.insert_after(NavigableString("~~"))
    for element in article.find_all(["br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"]):
        element.insert_before(NavigableString("\n"))
        element.insert_after(NavigableString("\n"))
    for element in article.find_all(["td", "th"]):
        element.insert_after(NavigableString(" | "))
    text = article.get_text(types=(NavigableString, TemplateString))
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not 40 <= len(text) or len(text.encode()) > MAX_DOCUMENT_BYTES:
        raise ValueError("source_article_empty_or_too_large")
    revision_match = re.search(r'"wgRevisionId"\s*:\s*(\d+)', html)
    revision = revision_match[1] if revision_match else ""
    canonical = article_url("https://zh.moegirl.org.cn/" + quote(title, safe="/:"))
    version_url = canonical + "?oldid=" + revision if revision else canonical
    return {"document": {
        "name": re.sub(r"[/\\\x00-\x1f]", "_", title)[:170] + ".txt",
        "title": (title + " · 萌娘百科原文")[:200], "text": text, "category": "reference",
        "sources": [version_url, canonical], "tags": [title, "萌娘百科"],
        "attribution": "萌娘百科《" + title + "》贡献者；作者列表见页面历史：" + canonical + "?action=history",
        "license": LICENSE, "source_revision": revision,
        "source_retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source_format": "html_text", "source_sha256": hashlib.sha256(html.encode()).hexdigest(),
    }, "note": "取自公开网页，移除样式与导航，保留正文原句及删除线标记；未经 AI 改写。百科说法作为参考来源，事实、玩梗和二创按原文语境区分。图片不随文本转载。",
       "license_url": LICENSE_URL}


async def fetch_moegirl(url: str) -> dict:
    target = article_url(url)
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, trust_env=False,
            headers={"User-Agent": "XZBot/0.3 (private knowledge import)", "Accept": "text/html"}) as client:
            # Only normal public article redirects on the same fixed origin.
            for _ in range(3):
                async with client.stream("GET", target) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location", "")
                        if location.startswith("/") and not location.startswith("//"):
                            location = "https://zh.moegirl.org.cn" + location
                        target = article_url(location)
                        continue
                    if response.status_code != 200 or "text/html" not in response.headers.get("content-type", ""):
                        raise ValueError("source_access_restricted")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_PAGE_BYTES:
                            raise ValueError("source_page_too_large")
                try:
                    html = data.decode("utf-8")
                except UnicodeError:
                    raise ValueError("source_page_encoding_invalid") from None
                return parse_moegirl(html, target)
            raise ValueError("source_redirect_limit")
    except httpx.HTTPError:
        raise ValueError("source_unavailable") from None
