#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import re
import ssl
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

BASE = "https://www.dls.gov.ua"
ARCHIVE_URL = f"{BASE}/projects_reg_acts/"
LONG_SECTION_URL = (
    "https://www.dls.gov.ua/%d0%bd%d0%be%d1%80%d0%bc%d0%b0%d1%82%d0%b8%d0%b2%d0%bd%d1%96-"
    "%d0%b4%d0%be%d0%ba%d1%83%d0%bc%d0%b5%d0%bd%d1%82%d0%b8/%d1%80%d0%b5%d0%b3%d1%83%d0%bb"
    "%d1%8f%d1%82%d0%be%d1%80%d0%bd%d0%b0-%d0%b4%d1%96%d1%8f%d0%bb%d1%8c%d0%bd%d1%96%d1%81"
    "%d1%82%d1%8c/%d0%bf%d1%80%d0%be%d0%b5%d0%ba%d1%82%d0%b8/"
)
REPORT_DIR = Path("diagnostics")
MAX_PAGE_BYTES = 2_000_000
MAX_BINARY_BYTES = 16_384
TIMEOUT_SECONDS = 30
POST_SAMPLE_LIMIT = 3
ATTACHMENT_SAMPLE_LIMIT = 6

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.7,en;q=0.6",
    "Accept-Encoding": "identity",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

ATTACHMENT_RE = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?|zip)(?:$|[?#])", re.I)
SPACE_RE = re.compile(r"\s+")


def normalize_request_url(url: str) -> str:
    """
    Convert a browser-style URL into an ASCII-safe URL suitable for urllib.

    The DLS pages sometimes contain attachment href values with literal
    Cyrillic characters or spaces. Browsers encode them automatically,
    while urllib/http.client requires an ASCII request target.
    """
    value = html.unescape((url or "").strip())
    parsed = urlparse(value)

    hostname = parsed.hostname or ""
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        ascii_hostname = hostname

    netloc = ascii_hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username:
        userinfo = quote(parsed.username, safe="")
        if parsed.password:
            userinfo += ":" + quote(parsed.password, safe="")
        netloc = f"{userinfo}@{netloc}"

    # Keep existing percent escapes and standard URL delimiters intact.
    path = quote(parsed.path, safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="=&%:@/?+,;'-._~")

    return urlunparse(
        (parsed.scheme, netloc, path, parsed.params, query, "")
    )


class RecordingRedirectHandler(HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.history: list[dict[str, object]] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        self.history.append(
            {
                "status": int(code),
                "from": req.full_url,
                "to": newurl,
            }
        )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        self._href = attr_map.get("href", "")
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        text = normalize_text(" ".join(self._parts))
        self.links.append({"href": self._href.strip(), "text": text})
        self._href = None
        self._parts = []


@dataclass
class ProbeResult:
    label: str
    requested_url: str
    method: str
    status: int | None
    final_url: str | None
    content_type: str | None
    content_length_header: str | None
    bytes_read: int
    body_truncated: bool
    elapsed_ms: int
    redirect_history: list[dict[str, object]]
    server: str | None
    classification: str
    error: str | None
    preview: str
    body_text: str | None = None


def normalize_text(value: str) -> str:
    return SPACE_RE.sub(" ", html.unescape(value or "")).strip()


def decode_bytes(data: bytes, content_type: str | None) -> str:
    charset = None
    if content_type:
        match = re.search(r"charset=([\w.-]+)", content_type, flags=re.I)
        if match:
            charset = match.group(1)
    for encoding in [charset, "utf-8", "cp1251", "latin-1"]:
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def classify(status: int | None, content_type: str | None, text: str, error: str | None) -> str:
    low = text.lower()
    if error and status is None:
        return "NETWORK_ERROR"
    if any(marker in low for marker in ["just a moment", "cf-chl-", "cloudflare ray id", "attention required"]):
        return "BLOCKED_CLOUDFLARE"
    if status == 404:
        return "NOT_FOUND"
    if status is not None and status >= 400:
        return f"HTTP_{status}"
    ctype = (content_type or "").lower()
    if "json" in ctype or text.lstrip().startswith(("{", "[")):
        return "OK_JSON"
    if "xml" in ctype or text.lstrip().startswith("<?xml"):
        if "<rss" in low or "<feed" in low:
            return "OK_RSS"
        if "<urlset" in low or "<sitemapindex" in low:
            return "OK_SITEMAP"
        return "OK_XML"
    if "html" in ctype or "<!doctype html" in low or "<html" in low:
        return "OK_HTML"
    if status is not None and 200 <= status < 300:
        return "OK_BINARY_OR_OTHER"
    return "OTHER"


def probe(
    label: str,
    url: str,
    *,
    max_bytes: int = MAX_PAGE_BYTES,
    range_request: bool = False,
    include_body_text: bool = True,
) -> ProbeResult:
    redirect_handler = RecordingRedirectHandler()
    context = ssl.create_default_context()
    opener = build_opener(redirect_handler, HTTPSHandler(context=context))

    request_url = normalize_request_url(url)
    headers = dict(BROWSER_HEADERS)
    if range_request:
        headers["Range"] = f"bytes=0-{max_bytes - 1}"
        headers["Accept"] = "*/*"

    started = time.monotonic()
    status: int | None = None
    final_url: str | None = None
    content_type: str | None = None
    content_length_header: str | None = None
    server: str | None = None
    error: str | None = None
    data = b""
    truncated = False

    try:
        request = Request(request_url, headers=headers, method="GET")
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            status = int(response.getcode())
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type")
            content_length_header = response.headers.get("Content-Length")
            server = response.headers.get("Server")
            data = response.read(max_bytes + 1)
            truncated = len(data) > max_bytes
            data = data[:max_bytes]
    except HTTPError as exc:
        status = int(exc.code)
        final_url = exc.geturl()
        content_type = exc.headers.get("Content-Type") if exc.headers else None
        content_length_header = exc.headers.get("Content-Length") if exc.headers else None
        server = exc.headers.get("Server") if exc.headers else None
        data = exc.read(max_bytes + 1)
        truncated = len(data) > max_bytes
        data = data[:max_bytes]
        error = f"HTTPError: {exc}"
    except (URLError, TimeoutError, OSError, UnicodeError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"

    elapsed_ms = int((time.monotonic() - started) * 1000)
    text = decode_bytes(data, content_type)
    preview = normalize_text(text)
    classification = classify(status, content_type, text, error)

    return ProbeResult(
        label=label,
        requested_url=request_url,
        method="GET_RANGE" if range_request else "GET",
        status=status,
        final_url=final_url,
        content_type=content_type,
        content_length_header=content_length_header,
        bytes_read=len(data),
        body_truncated=truncated,
        elapsed_ms=elapsed_ms,
        redirect_history=redirect_handler.history,
        server=server,
        classification=classification,
        error=error,
        preview=preview,
        body_text=text if include_body_text else None,
    )

def extract_links(page_url: str, html_text: str) -> list[dict[str, str]]:
    parser = LinkCollector()
    try:
        parser.feed(html_text)
    except Exception:
        pass
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in parser.links:
        raw = item.get("href", "")
        if not raw or raw.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(page_url, raw)
        parsed = urlparse(absolute)
        cleaned = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ""))
        if cleaned in seen:
            continue
        seen.add(cleaned)
        result.append({"url": cleaned, "text": item.get("text", "")})
    return result


def is_post_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") + "/"
    if host != "dls.gov.ua":
        return False
    if not path.startswith("/projects_reg_acts/"):
        return False
    if path in {"/projects_reg_acts/", "/projects_reg_acts/feed/"}:
        return False
    if "/page/" in path:
        return False
    return True


def unique_urls(items: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    output: list[dict[str, str]] = []
    for item in items:
        url = item["url"]
        if url in seen:
            continue
        seen.add(url)
        output.append(item)
    return output


def safe_cell(value: object) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def make_markdown(report: dict[str, object]) -> str:
    probes = report["probes"]
    assert isinstance(probes, list)
    summary = report["summary"]
    assert isinstance(summary, dict)

    lines = [
        "# Діагностика джерел Держлікслужби",
        "",
        f"- Запуск: `{report['generated_at']}`",
        f"- Перевірено запитів: **{summary['total_probes']}**",
        f"- Успішних відповідей: **{summary['successful_probes']}**",
        f"- Заблокованих/помилкових: **{summary['failed_probes']}**",
        f"- Знайдено URL окремих публікацій: **{summary['post_urls_found']}**",
        f"- Знайдено вкладень: **{summary['attachment_urls_found']}**",
        "",
        "## Результати HTTP-перевірки",
        "",
        "| Перевірка | HTTP | Класифікація | Content-Type | Байт прочитано | Час, мс | Кінцева адреса |",
        "|---|---:|---|---|---:|---:|---|",
    ]
    for item in probes:
        assert isinstance(item, dict)
        lines.append(
            "| {label} | {status} | {classification} | {content_type} | {bytes_read} | {elapsed_ms} | {final_url} |".format(
                label=safe_cell(item.get("label")),
                status=safe_cell(item.get("status")),
                classification=safe_cell(item.get("classification")),
                content_type=safe_cell(item.get("content_type")),
                bytes_read=safe_cell(item.get("bytes_read")),
                elapsed_ms=safe_cell(item.get("elapsed_ms")),
                final_url=safe_cell(item.get("final_url")),
            )
        )

    lines += ["", "## Виявлені окремі публікації", ""]
    post_urls = report.get("post_urls", [])
    if isinstance(post_urls, list) and post_urls:
        for item in post_urls[:20]:
            if isinstance(item, dict):
                lines.append(f"- [{item.get('text') or item.get('url')}]({item.get('url')})")
    else:
        lines.append("Окремі публікації не виявлено у доступній HTML-відповіді.")

    lines += ["", "## Виявлені вкладення", ""]
    attachments = report.get("attachment_urls", [])
    if isinstance(attachments, list) and attachments:
        for item in attachments[:30]:
            if isinstance(item, dict):
                lines.append(f"- [{item.get('text') or item.get('url')}]({item.get('url')})")
    else:
        lines.append("DOC/DOCX/PDF/RTF/XLS/XLSX/ZIP-посилання не виявлено.")

    lines += ["", "## Фрагменти відповідей", ""]
    for item in probes:
        assert isinstance(item, dict)
        preview = str(item.get("preview") or "")
        if not preview:
            continue
        lines += [f"### {item.get('label')}", "", "```text", preview[:1000], "```", ""]

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    static_targets = [
        ("Довга адреса розділу", LONG_SECTION_URL),
        ("Короткий HTML-архів", ARCHIVE_URL),
        ("Друга сторінка архіву", f"{ARCHIVE_URL}page/2/"),
        ("robots.txt", f"{BASE}/robots.txt"),
        ("WordPress REST index", f"{BASE}/wp-json/"),
        ("WordPress REST types", f"{BASE}/wp-json/wp/v2/types"),
        ("REST custom post type", f"{BASE}/wp-json/wp/v2/projects_reg_acts?per_page=5&_fields=id,date,link,slug,title"),
        ("REST search subtype", f"{BASE}/wp-json/wp/v2/search?subtype=projects_reg_acts&per_page=5"),
        ("RSS custom post type", f"{BASE}/feed/?post_type=projects_reg_acts"),
        ("RSS архіву", f"{ARCHIVE_URL}feed/"),
        ("WordPress sitemap index", f"{BASE}/wp-sitemap.xml"),
        ("Sitemap custom post type", f"{BASE}/wp-sitemap-posts-projects_reg_acts-1.xml"),
    ]

    results: list[ProbeResult] = []
    for label, url in static_targets:
        print(f"\n=== {label} ===\n{url}")
        result = probe(label, url)
        results.append(result)
        print(
            f"status={result.status} class={result.classification} "
            f"type={result.content_type} bytes={result.bytes_read} "
            f"time={result.elapsed_ms}ms final={result.final_url}"
        )
        if result.error:
            print(f"error={result.error}")
        print(f"preview={result.preview[:400]}")

    archive_result = next((r for r in results if r.label == "Короткий HTML-архів"), None)
    archive_links: list[dict[str, str]] = []
    if archive_result and archive_result.body_text and archive_result.classification == "OK_HTML":
        archive_links = extract_links(archive_result.final_url or ARCHIVE_URL, archive_result.body_text)

    post_urls = unique_urls(item for item in archive_links if is_post_url(item["url"]))
    print(f"\nЗнайдено URL окремих публікацій: {len(post_urls)}")

    all_page_links: list[dict[str, str]] = []
    for index, item in enumerate(post_urls[:POST_SAMPLE_LIMIT], start=1):
        label = f"Окрема публікація #{index}"
        result = probe(label, item["url"])
        results.append(result)
        print(
            f"{label}: status={result.status} class={result.classification} "
            f"type={result.content_type} bytes={result.bytes_read}"
        )
        if result.body_text and result.classification == "OK_HTML":
            all_page_links.extend(extract_links(result.final_url or item["url"], result.body_text))

    all_page_links = unique_urls(all_page_links)
    attachment_urls = unique_urls(item for item in all_page_links if ATTACHMENT_RE.search(item["url"]))
    print(f"Знайдено вкладень: {len(attachment_urls)}")

    for index, item in enumerate(attachment_urls[:ATTACHMENT_SAMPLE_LIMIT], start=1):
        label = f"Вкладення #{index}"
        result = probe(
            label,
            item["url"],
            max_bytes=MAX_BINARY_BYTES,
            range_request=True,
            include_body_text=False,
        )
        results.append(result)
        print(
            f"{label}: status={result.status} class={result.classification} "
            f"type={result.content_type} bytes={result.bytes_read}"
        )

    print_links = [
        item
        for item in all_page_links
        if "print" in item["url"].lower() or "версія для друку" in item.get("text", "").lower()
    ]
    print_links = unique_urls(print_links)
    if print_links:
        result = probe("Версія для друку", print_links[0]["url"])
        results.append(result)
        print(
            f"Версія для друку: status={result.status} class={result.classification} "
            f"type={result.content_type} bytes={result.bytes_read}"
        )

    serializable_probes: list[dict[str, object]] = []
    for result in results:
        item = asdict(result)
        item.pop("body_text", None)
        serializable_probes.append(item)

    successful = sum(
        1
        for result in results
        if result.classification.startswith("OK_") and result.status is not None and result.status < 400
    )
    report: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE,
        "archive_url": ARCHIVE_URL,
        "summary": {
            "total_probes": len(results),
            "successful_probes": successful,
            "failed_probes": len(results) - successful,
            "post_urls_found": len(post_urls),
            "attachment_urls_found": len(attachment_urls),
            "rest_custom_post_type_available": any(
                r.label == "REST custom post type" and r.classification == "OK_JSON" and r.status == 200
                for r in results
            ),
            "rss_available": any(r.classification == "OK_RSS" and r.status == 200 for r in results),
            "sitemap_available": any(r.classification == "OK_SITEMAP" and r.status == 200 for r in results),
            "archive_html_available": bool(
                archive_result and archive_result.classification == "OK_HTML" and archive_result.status == 200
            ),
            "attachments_downloadable": any(
                r.label.startswith("Вкладення #") and r.status in {200, 206}
                for r in results
            ),
        },
        "post_urls": post_urls,
        "attachment_urls": attachment_urls,
        "print_links": print_links,
        "probes": serializable_probes,
    }

    json_path = REPORT_DIR / "diagnose_dls_sources.json"
    md_path = REPORT_DIR / "diagnose_dls_sources.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(make_markdown(report), encoding="utf-8")

    print(f"\nJSON-звіт: {json_path}")
    print(f"Markdown-звіт: {md_path}")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Перервано користувачем", file=sys.stderr)
        raise SystemExit(130)
