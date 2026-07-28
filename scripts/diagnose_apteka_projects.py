from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag

BASE = "https://www.apteka.ua"
CATEGORY = f"{BASE}/category/moz"
REPORT_DIR = Path("reports")
TZ = ZoneInfo("Europe/Kyiv")
TIMEOUT = 35

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.7,en;q=0.6",
    "Cache-Control": "no-cache",
}

MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4,
    "травня": 5, "червня": 6, "липня": 7, "серпня": 8,
    "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}

UA_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\s*(?:р\.|року)?",
    re.I,
)
DOT_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4})(?!\d)")
ARTICLE_RE = re.compile(r"^https?://(?:www\.)?apteka\.ua/article/(\d+)/?$", re.I)
PROJECT_TITLE_RE = re.compile(r"^\s*(?:проєкт|проект)\b", re.I)
PROJECT_BODY_RE = re.compile(
    r"(?:на\s+громадське\s+обговорення\s+пропонується|"
    r"повідомлення\s+про\s+оприлюднення).{0,300}\b(?:проєкт|проект)\b",
    re.I | re.S,
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-яІіЇїЄє]{2,}")
PHONE_RE = re.compile(r"(?:\+?38[\s-]?)?\(?0\d{2}\)?[\s-]?\d{2,3}[\s-]?\d{2}[\s-]?\d{2}")
FILE_RE = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?|zip)(?:$|[?#])", re.I)

OFFICIAL_DATE_PATTERNS = [
    re.compile(
        r"(?:опублікован[оийаі]*|оприлюднен[оийаі]*).{0,100}?"
        r"(\d{1,2}[./]\d{1,2}[./]\d{4})",
        re.I | re.S,
    ),
    re.compile(
        r"(?:із|з)\s*(\d{1,2}[./]\d{1,2}[./]\d{4})\s*"
        r"(?:р\.?\s*)?(?:по|до|—|-)\s*\d{1,2}[./]\d{1,2}[./]\d{4}",
        re.I,
    ),
]
RANGE_RE = re.compile(
    r"(?:із|з)\s*(\d{1,2}[./]\d{1,2}[./]\d{4})\s*"
    r"(?:р\.?\s*)?(?:по|до|—|-)\s*(\d{1,2}[./]\d{1,2}[./]\d{4})",
    re.I,
)
UNTIL_RE = re.compile(
    r"(?:пропозиці\w*|зауважен\w*).{0,250}?\bдо\s+"
    r"(\d{1,2}[./]\d{1,2}[./]\d{4})",
    re.I | re.S,
)
DURATION_RE = re.compile(r"протягом\s+(\d{1,3})\s+(?:календарних\s+)?дн", re.I)


@dataclass
class Probe:
    label: str
    url: str
    status: int | None
    final_url: str | None
    content_type: str | None
    elapsed_ms: int
    bytes_read: int
    classification: str
    error: str | None = None


@dataclass
class Article:
    article_id: str
    title: str
    article_url: str
    apteka_publication_date: str | None = None
    official_publication_date: str | None = None
    official_date_candidates: list[str] = field(default_factory=list)
    selection_date: str | None = None
    selected: bool = False
    selected_by_apteka_date: bool = False
    selected_by_official_date: bool = False
    selection_reason: str = ""
    project_reason: str = ""
    deadline_date: str | None = None
    deadline_status: str = "NOT_FOUND"
    days_until_deadline: int | None = None
    deadline_context: str = ""
    contact_person: str = ""
    contact_emails: list[str] = field(default_factory=list)
    contact_phones: list[str] = field(default_factory=list)
    official_links: list[str] = field(default_factory=list)
    document_links: list[str] = field(default_factory=list)
    article_text_chars: int = 0
    article_preview: str = ""
    warnings: list[str] = field(default_factory=list)
    http_status: int | None = None
    content_type: str | None = None


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def uniq(values: list[str]) -> list[str]:
    result, seen = [], set()
    for value in values:
        value = clean(value)
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int((os.getenv(name) or "").strip() or default)
    except ValueError:
        value = default
    return max(low, min(high, value))


def iso_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def dot_date(value: str | None) -> date | None:
    match = DOT_DATE_RE.search(value or "")
    if not match:
        return None
    day, month, year = map(int, match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def ua_date(value: str | None) -> date | None:
    match = UA_DATE_RE.search(value or "")
    if not match:
        return None
    try:
        return date(int(match.group(3)), MONTHS[match.group(2).lower()], int(match.group(1)))
    except ValueError:
        return None


def iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def uk(value: str | None) -> str:
    parsed = iso_date(value)
    return parsed.strftime("%d.%m.%Y") if parsed else (value or "—")


def classify(status: int | None, content_type: str | None, text: str, error: str | None) -> str:
    if error:
        return "REQUEST_ERROR"
    if status is None:
        return "NO_STATUS"
    lowered = (text or "").lower()
    if status in {403, 503} and ("just a moment" in lowered or "cf-chl-" in lowered):
        return "CLOUDFLARE_BLOCK"
    if status >= 400:
        return f"HTTP_{status}"
    ctype = (content_type or "").lower()
    if "json" in ctype:
        return "OK_JSON"
    if "xml" in ctype or text.lstrip().startswith("<?xml"):
        return "OK_XML"
    if "html" in ctype or "<html" in lowered:
        return "OK_HTML"
    return "OK_OTHER"


def fetch(session: requests.Session, label: str, url: str, max_bytes: int = 3_000_000) -> tuple[Probe, str]:
    started = time.monotonic()
    status = final_url = content_type = error = None
    body = b""
    text = ""
    try:
        response = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        status = response.status_code
        final_url = response.url
        content_type = response.headers.get("Content-Type")
        body = response.content[:max_bytes]
        encoding = response.encoding or response.apparent_encoding or "utf-8"
        text = body.decode(encoding, errors="replace")
    except requests.RequestException as exc:
        error = f"{type(exc).__name__}: {exc}"
    probe = Probe(
        label=label,
        url=url,
        status=status,
        final_url=final_url,
        content_type=content_type,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        bytes_read=len(body),
        classification=classify(status, content_type, text, error),
        error=error,
    )
    return probe, text


def article_id(url: str) -> str:
    match = re.search(r"/article/(\d+)", url)
    return match.group(1) if match else ""


def category_date_after_title(link: Tag) -> tuple[date | None, str]:
    """
    Read the category publication date only from nodes that follow the article
    heading. Dates inside the title itself must never be treated as publication
    dates.
    """
    heading = link.find_parent(["h1", "h2", "h3", "h4", "h5", "h6"])

    start_nodes: list[Tag] = []
    if isinstance(heading, Tag):
        start_nodes.append(heading)
        if isinstance(heading.parent, Tag):
            start_nodes.append(heading.parent)
    elif isinstance(link.parent, Tag):
        start_nodes.append(link.parent)

    checked: set[int] = set()

    for start_node in start_nodes:
        if id(start_node) in checked:
            continue
        checked.add(id(start_node))

        collected: list[str] = []
        char_count = 0
        sibling_count = 0

        for sibling in start_node.next_siblings:
            sibling_count += 1
            if sibling_count > 12:
                break

            if isinstance(sibling, Tag):
                # Stop at the next article heading/card.
                if sibling.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                    other = sibling.find("a", href=True)
                    if other:
                        other_url = urljoin(BASE, other.get("href", ""))
                        if ARTICLE_RE.match(other_url.rstrip("/") + "/"):
                            break

                other = sibling.find("a", href=True)
                if other:
                    other_url = urljoin(BASE, other.get("href", ""))
                    if ARTICLE_RE.match(other_url.rstrip("/") + "/"):
                        break

                time_tag = sibling if sibling.name == "time" else sibling.find("time")
                if isinstance(time_tag, Tag):
                    raw = str(time_tag.get("datetime") or time_tag.get_text(" ", strip=True))
                    parsed = iso_date(raw[:10]) or ua_date(raw)
                    if parsed:
                        return parsed, clean(raw)

                value = clean(sibling.get_text(" ", strip=True))
            else:
                value = clean(str(sibling))

            if not value:
                continue

            collected.append(value)
            char_count += len(value) + 1
            combined = clean(" ".join(collected))
            parsed = ua_date(combined)
            if parsed:
                return parsed, combined[:500]

            if char_count >= 700:
                break

    # Conservative fallback: remove the exact normalized title from a compact
    # parent container before looking for a date.
    title = clean(link.get_text(" ", strip=True))
    current: Tag | None = heading if isinstance(heading, Tag) else link
    for _ in range(5):
        parent = current.parent if current else None
        if not isinstance(parent, Tag):
            break
        parent_text = clean(parent.get_text(" ", strip=True))
        if len(parent_text) > 1800:
            current = parent
            continue

        if parent_text.startswith(title):
            without_title = clean(parent_text[len(title):])
        else:
            without_title = clean(parent_text.replace(title, "", 1))

        parsed = ua_date(without_title)
        if parsed:
            return parsed, without_title[:500]
        current = parent

    return None, ""


def parse_category(page_html: str) -> list[dict]:
    soup = BeautifulSoup(page_html, "lxml")
    results, seen = [], set()
    for link in soup.find_all("a", href=True):
        absolute = urljoin(BASE, link.get("href", ""))
        if not ARTICLE_RE.match(absolute.rstrip("/") + "/") or absolute in seen:
            continue
        title = clean(link.get_text(" ", strip=True))
        if not title:
            continue

        published, date_context = category_date_after_title(link)

        results.append({
            "article_id": article_id(absolute),
            "title": title,
            "url": absolute,
            "card_date": iso(published),
            "card_date_context": date_context,
            "title_is_project": bool(PROJECT_TITLE_RE.search(title)),
        })
        seen.add(absolute)
    return results


def content_root(soup: BeautifulSoup) -> Tag:
    candidates: list[Tag] = []
    for selector in ["article", ".entry-content", ".post-content", ".article-content", "main"]:
        candidates.extend(item for item in soup.select(selector) if isinstance(item, Tag))
    h1 = soup.find("h1")
    if isinstance(h1, Tag):
        current: Tag | None = h1
        for _ in range(7):
            parent = current.parent if current else None
            if not isinstance(parent, Tag):
                break
            candidates.append(parent)
            current = parent
    body = soup.body if isinstance(soup.body, Tag) else soup
    candidates.append(body)

    scored: list[tuple[int, Tag]] = []
    for candidate in candidates:
        text = clean(candidate.get_text(" ", strip=True))
        if len(text) < 300:
            continue
        score = len(text)
        if candidate.name == "body":
            score -= 10000
        if candidate.find("h1"):
            score += 2000
        if re.search(r"повідомлення\s+про\s+оприлюднення", text, re.I):
            score += 4000
        scored.append((score, candidate))
    return max(scored, key=lambda item: item[0])[1] if scored else body


def article_pub_date(soup: BeautifulSoup, root_text: str) -> date | None:
    # Primary and reliable source on Apteka.ua.
    for tag in soup.find_all("time"):
        raw = str(tag.get("datetime") or tag.get_text(" ", strip=True))
        parsed = iso_date(raw[:10]) or ua_date(raw)
        if parsed:
            return parsed

    # Fallback: inspect text after h1, never the title itself.
    h1 = soup.find("h1")
    if isinstance(h1, Tag):
        collected: list[str] = []
        for node in h1.next_elements:
            if node is h1:
                continue
            if isinstance(node, Tag) and node.name == "h1":
                break
            if isinstance(node, str):
                value = clean(node)
                if not value:
                    continue
                collected.append(value)
                combined = clean(" ".join(collected))
                parsed = ua_date(combined)
                if parsed:
                    return parsed
                if len(combined) >= 700:
                    break

        title = clean(h1.get_text(" ", strip=True))
        current: Tag | None = h1
        for _ in range(5):
            parent = current.parent if current else None
            if not isinstance(parent, Tag):
                break
            parent_text = clean(parent.get_text(" ", strip=True))
            parsed = ua_date(clean(parent_text.replace(title, "", 1)))
            if parsed:
                return parsed
            current = parent

    return None


def official_candidates(text: str) -> list[date]:
    found: list[date] = []
    for pattern in OFFICIAL_DATE_PATTERNS:
        for match in pattern.finditer(text):
            parsed = dot_date(match.group(1))
            if parsed and parsed not in found:
                found.append(parsed)
    for match in RANGE_RE.finditer(text):
        context = text[max(0, match.start() - 300):match.end() + 100]
        if re.search(r"пропозиці|зауважен|оприлюднен|публікац", context, re.I):
            parsed = dot_date(match.group(1))
            if parsed and parsed not in found:
                found.append(parsed)
    return found


def choose_official(candidates: list[date], article_date: date | None, as_of: date, warnings: list[str]) -> date | None:
    plausible: list[date] = []
    for candidate in candidates:
        if candidate > as_of + timedelta(days=1):
            warnings.append(f"Офіційна дата у майбутньому: {candidate.isoformat()}")
            continue
        if article_date and abs((candidate - article_date).days) > 180:
            warnings.append(f"Підозріла офіційна дата: {candidate.isoformat()}")
            continue
        plausible.append(candidate)
    if not plausible:
        return None
    if article_date:
        plausible.sort(key=lambda item: abs((item - article_date).days))
    return plausible[0]


def deadline_window(text: str) -> str:
    matches = list(re.finditer(r"пропозиці\w*|зауважен\w*", text, re.I))
    if not matches:
        return text[:2500]
    return " ".join(text[max(0, m.start() - 180):m.start() + 1000] for m in matches[:5])


def deadline(text: str, official_date: date | None) -> tuple[date | None, str, str]:
    window = deadline_window(text)
    match = RANGE_RE.search(window)
    if match:
        value = dot_date(match.group(2))
        context = clean(window[max(0, match.start() - 180):match.end() + 260])
        if value:
            return value, "EXPLICIT_RANGE", context
    match = UNTIL_RE.search(window)
    if match:
        value = dot_date(match.group(1))
        context = clean(window[max(0, match.start() - 180):match.end() + 260])
        if value:
            return value, "EXPLICIT_UNTIL", context
    match = DURATION_RE.search(window)
    if match and official_date:
        value = official_date + timedelta(days=int(match.group(1)))
        context = clean(window[max(0, match.start() - 180):match.end() + 300])
        return value, "CALCULATED_APPROX", context
    return None, "NOT_FOUND", clean(window[:700])


def contact_person(text: str) -> str:
    for pattern in [
        re.compile(r"контактна\s+особа\s*:\s*([^,.;]{3,120})", re.I),
        re.compile(r"відповідальна\s+особа\s*:\s*([^,.;]{3,120})", re.I),
    ]:
        match = pattern.search(text)
        if match:
            return clean(match.group(1))
    return ""


def links(root: Tag) -> tuple[list[str], list[str]]:
    official, docs = [], []
    for anchor in root.find_all("a", href=True):
        absolute = urljoin(BASE, anchor.get("href", ""))
        host = urlparse(absolute).netloc.lower()
        if FILE_RE.search(absolute):
            docs.append(absolute)
        if (host.endswith(".gov.ua") or host == "gov.ua") and "apteka.ua" not in host:
            official.append(absolute)
    return uniq(official), uniq(docs)


def analyze(session: requests.Session, candidate: dict, as_of: date, start: date, basis: str, probes: list[Probe]) -> Article:
    item = Article(
        article_id=candidate.get("article_id") or article_id(candidate["url"]),
        title=candidate.get("title") or "",
        article_url=candidate["url"],
        apteka_publication_date=candidate.get("card_date"),
    )
    probe, page = fetch(session, f"article:{item.article_id}", item.article_url, 5_000_000)
    probes.append(probe)
    item.http_status, item.content_type = probe.status, probe.content_type
    if probe.status != 200 or probe.classification != "OK_HTML":
        item.warnings.append(f"Сторінку не прочитано: {probe.classification}")
        return item

    soup = BeautifulSoup(page, "lxml")
    h1 = soup.find("h1")
    if isinstance(h1, Tag):
        item.title = clean(h1.get_text(" ", strip=True)) or item.title
    root = content_root(soup)
    text = clean(root.get_text(" ", strip=True))
    item.article_text_chars = len(text)
    item.article_preview = text[:900]

    article_date = article_pub_date(soup, text)
    if article_date:
        item.apteka_publication_date = article_date.isoformat()

    title_project = bool(PROJECT_TITLE_RE.search(item.title))
    body_project = bool(PROJECT_BODY_RE.search(text[:7000]))
    if title_project:
        item.project_reason = "TITLE_PROJECT"
    elif body_project:
        item.project_reason = "BODY_PUBLIC_CONSULTATION"
    else:
        item.project_reason = "NOT_PROJECT"
        item.selection_reason = "Відхилено: немає ознак проєкту НПА"
        return item

    candidates = official_candidates(text)
    item.official_date_candidates = [value.isoformat() for value in candidates]
    official = choose_official(candidates, article_date, as_of, item.warnings)
    item.official_publication_date = iso(official)

    end, status, context = deadline(text, official)
    item.deadline_date, item.deadline_status, item.deadline_context = iso(end), status, context
    if end:
        item.days_until_deadline = (end - as_of).days
        if end < as_of:
            item.warnings.append("Строк подання пропозицій уже минув")
    elif re.search(r"протягом\s+\d+\s+дн", text, re.I):
        item.warnings.append("Є тривалість обговорення, але не визначено точну початкову дату")

    item.contact_emails = uniq(EMAIL_RE.findall(str(root)))
    item.contact_phones = uniq(PHONE_RE.findall(text))
    item.contact_person = contact_person(text)
    item.official_links, item.document_links = links(root)

    article_date = iso_date(item.apteka_publication_date)
    official = iso_date(item.official_publication_date)
    item.selected_by_apteka_date = bool(article_date and start <= article_date <= as_of)
    item.selected_by_official_date = bool(official and start <= official <= as_of)

    if basis == "official_or_apteka":
        selection_date = official or article_date
        item.selection_reason = "Офіційна дата" if official else "Fallback: дата Apteka.ua"
    else:
        selection_date = article_date
        item.selection_reason = "Дата публікації Apteka.ua"
    item.selection_date = iso(selection_date)
    item.selected = bool(selection_date and start <= selection_date <= as_of)
    if not item.selected:
        item.selection_reason += "; поза періодом"
    if not item.official_publication_date:
        item.warnings.append("Не визначено надійну офіційну дату")
    if not item.deadline_date:
        item.warnings.append("Не визначено точну дату завершення обговорення")
    return item


def h(value: object) -> str:
    return html.escape(str(value or "—"), quote=True)


def deadline_label(item: Article) -> str:
    if not item.deadline_date:
        return '<span style="color:#9a3412;font-weight:600;">не визначено</span>'
    days = item.days_until_deadline
    if days is None:
        color, label = "#333", uk(item.deadline_date)
    elif days < 0:
        color, label = "#6b7280", f"{uk(item.deadline_date)} (минув)"
    elif days <= 3:
        color, label = "#b91c1c", f"{uk(item.deadline_date)} ({days} дн.)"
    elif days <= 7:
        color, label = "#b45309", f"{uk(item.deadline_date)} ({days} дн.)"
    else:
        color, label = "#166534", f"{uk(item.deadline_date)} ({days} дн.)"
    return f'<span style="color:{color};font-weight:700;">{h(label)}</span>'


def html_report(selected: list[Article], as_of: date, start: date, basis: str) -> str:
    rows = []
    for item in selected:
        source_links = [f'<div><a href="{h(item.article_url)}">Публікація</a></div>']
        if item.official_links:
            source_links.append(f'<div><a href="{h(item.official_links[0])}">Офіційне джерело</a></div>')
        if item.document_links:
            source_links.append(f'<div><a href="{h(item.document_links[0])}">Документ</a></div>')
        preview = item.article_preview[:520] + ("…" if len(item.article_preview) > 520 else "")
        warnings = ""
        if item.warnings:
            warnings = f'<div style="margin-top:8px;font-size:12px;color:#9a3412;">{h("; ".join(item.warnings[:3]))}</div>'
        rows.append(f"""
<tr>
<td style="border:1px solid #d9d9d9;padding:10px;vertical-align:top;">
<div><strong>Apteka.ua:</strong> {h(uk(item.apteka_publication_date))}</div>
<div><strong>Офіційно:</strong> {h(uk(item.official_publication_date))}</div>
<div style="margin-top:8px;"><strong>Пропозиції до:</strong><br>{deadline_label(item)}</div>
</td>
<td style="border:1px solid #d9d9d9;padding:10px;vertical-align:top;"><div style="font-weight:600;">{h(item.title)}</div>{warnings}</td>
<td style="border:1px solid #d9d9d9;padding:10px;vertical-align:top;">{h(preview)}<div style="margin-top:8px;font-size:12px;color:#666;">Діагностичний фрагмент, не AI Summary.</div></td>
<td style="border:1px solid #d9d9d9;padding:10px;vertical-align:top;"><div><strong>Контакт:</strong> {h(item.contact_person)}</div><div><strong>Email:</strong> {h(", ".join(item.contact_emails[:3]))}</div></td>
<td style="border:1px solid #d9d9d9;padding:10px;vertical-align:top;line-height:1.8;">{''.join(source_links)}</td>
</tr>""")
    body = "".join(rows) or '<tr><td colspan="5" style="border:1px solid #d9d9d9;padding:12px;">Проєктів за період не знайдено.</td></tr>'
    return f"""<!doctype html><html lang="uk"><head><meta charset="utf-8"><title>Apteka.ua diagnostics</title></head><body>
<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.45;color:#222;">
<h1 style="font-size:20px;margin:0 0 12px 0;">Моніторинг проєктів НПА у фармі — діагностичний звіт</h1>
<p>Період: <strong>{start.strftime('%d.%m.%Y')}</strong> – <strong>{as_of.strftime('%d.%m.%Y')}</strong></p>
<p>Відібрано: <strong>{len(selected)}</strong>. База відбору: <strong>{h(basis)}</strong>.</p>
<table style="width:100%;border-collapse:collapse;table-layout:fixed;font-family:Arial,sans-serif;font-size:14px;">
<colgroup><col style="width:17%;"><col style="width:25%;"><col style="width:29%;"><col style="width:17%;"><col style="width:12%;"></colgroup>
<thead><tr>
<th style="text-align:left;border:1px solid #d9d9d9;background:#f3f4f6;padding:10px;">Оприлюднення і строк</th>
<th style="text-align:left;border:1px solid #d9d9d9;background:#f3f4f6;padding:10px;">Назва проєкту</th>
<th style="text-align:left;border:1px solid #d9d9d9;background:#f3f4f6;padding:10px;">Зміст / майбутній AI Summary</th>
<th style="text-align:left;border:1px solid #d9d9d9;background:#f3f4f6;padding:10px;">Подання пропозицій</th>
<th style="text-align:left;border:1px solid #d9d9d9;background:#f3f4f6;padding:10px;">Джерела</th>
</tr></thead><tbody>{body}</tbody></table></div></body></html>"""


def cell(value: object) -> str:
    return clean(str(value or "—")).replace("|", "\\|")[:220]


def markdown(probes: list[Probe], articles: list[Article], as_of: date, start: date, basis: str, pages: list[dict]) -> str:
    selected = [item for item in articles if item.selected]
    lines = [
        "# Діагностика Apteka.ua — проєкти НПА", "",
        f"- Дата перевірки: **{as_of.strftime('%d.%m.%Y')}**",
        f"- Період: **{start.strftime('%d.%m.%Y')}–{as_of.strftime('%d.%m.%Y')}**",
        f"- База відбору: **{basis}**",
        f"- Сторінок категорії: **{len(pages)}**",
        f"- Детально перевірено статей: **{sum(1 for item in articles if item.http_status is not None)}**",
        f"- Відібрано проєктів: **{len(selected)}**",
        f"- Строк визначено: **{sum(1 for item in selected if item.deadline_date)}/{len(selected)}**",
        "", "## Перевірка джерел", "",
        "| Джерело | HTTP | Тип | Результат | Час, мс |",
        "|---|---:|---|---|---:|",
    ]
    for p in probes:
        lines.append(f"| {cell(p.label)} | {p.status or '—'} | {cell(p.content_type)} | {cell(p.classification)} | {p.elapsed_ms} |")
    lines += ["", "## Відібрані проєкти", "", "| Apteka.ua | Офіційно | Строк | Назва | Парсер |", "|---|---|---|---|---|"]
    for item in selected:
        lines.append(f"| {uk(item.apteka_publication_date)} | {uk(item.official_publication_date)} | {uk(item.deadline_date)} | [{cell(item.title)}]({item.article_url}) | {item.deadline_status} |")
    warnings = [item for item in articles if item.warnings and item.project_reason != "NOT_PROJECT"]
    if warnings:
        lines += ["", "## Попередження парсера", ""]
        for item in warnings:
            lines.append(f"- **{cell(item.title)}**: {cell('; '.join(item.warnings))}")
    lines += ["", "## Сторінки категорії", "", "| Сторінка | HTTP | Статей | Проєктів у назвах | Найновіша | Найстаріша |", "|---:|---:|---:|---:|---|---|"]
    for stat in pages:
        lines.append(f"| {stat['page']} | {stat.get('status') or '—'} | {stat['articles']} | {stat['project_titles']} | {stat.get('newest_date') or '—'} | {stat.get('oldest_date') or '—'} |")
    lines += ["", "Повний JSON і HTML-макет є в artifact `apteka-source-diagnostics`.", ""]
    return "\n".join(lines)


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    lookback = int_env("LOOKBACK_DAYS", 14, 1, 90)
    max_pages = int_env("MAX_PAGES", 5, 1, 30)
    basis = (os.getenv("DATE_BASIS") or "apteka").strip()
    if basis not in {"apteka", "official_or_apteka"}:
        basis = "apteka"
    raw_as_of = (os.getenv("AS_OF_DATE") or "").strip()
    as_of = iso_date(raw_as_of) if raw_as_of else datetime.now(TZ).date()
    if not as_of:
        raise ValueError("AS_OF_DATE має бути YYYY-MM-DD")
    start = as_of - timedelta(days=max(lookback - 1, 0))

    session = requests.Session()
    session.headers.update(HEADERS)
    probes: list[Probe] = []
    pages: list[dict] = []

    diagnostics = [
        ("category_moz", CATEGORY),
        ("wordpress_rest_root", f"{BASE}/wp-json/"),
        ("wordpress_categories_search", f"{BASE}/wp-json/wp/v2/categories?search=МОЗ&per_page=20"),
        ("category_feed", f"{CATEGORY}/feed/"),
        ("site_feed", f"{BASE}/feed/"),
        ("wp_sitemap", f"{BASE}/wp-sitemap.xml"),
        ("robots", f"{BASE}/robots.txt"),
    ]
    first_html = ""
    for label, url in diagnostics:
        probe, body = fetch(session, label, url)
        probes.append(probe)
        if label == "category_moz":
            first_html = body

    candidates: dict[str, dict] = {}
    reached_old = False
    for page in range(1, max_pages + 1):
        url = CATEGORY if page == 1 else f"{CATEGORY}/page/{page}/"
        if page == 1:
            body = first_html
            probe = next((p for p in probes if p.label == "category_moz"), None)
        else:
            probe, body = fetch(session, f"category_page_{page}", url)
            probes.append(probe)
        parsed = parse_category(body) if probe and probe.status == 200 else []
        dates = sorted(d for d in (iso_date(item.get("card_date")) for item in parsed) if d)
        pages.append({
            "page": page,
            "url": url,
            "status": probe.status if probe else None,
            "articles": len(parsed),
            "project_titles": sum(1 for item in parsed if item["title_is_project"]),
            "newest_date": iso(dates[-1]) if dates else None,
            "oldest_date": iso(dates[0]) if dates else None,
        })
        for item in parsed:
            candidates.setdefault(item["url"], item)
        if not parsed:
            break

        # Do not stop early by a date extracted from a category card.
        # A title may itself contain an old legal-act date. The authoritative
        # Apteka.ua publication date is verified later on the article page.
        if dates and dates[0] < start:
            reached_old = True

    candidate_list = sorted(
        candidates.values(),
        key=lambda item: item.get("card_date") or "",
        reverse=True,
    )

    # Open all project-titled articles from the checked pages. Filtering by the
    # 14-day window happens only after reading the article's own <time> value.
    details = [
        candidate
        for candidate in candidate_list
        if candidate.get("title_is_project")
    ][:60]

    articles = [analyze(session, candidate, as_of, start, basis, probes) for candidate in details]
    opened = {item.article_url for item in articles}
    for candidate in candidate_list:
        if candidate["url"] in opened:
            continue
        articles.append(Article(
            article_id=candidate.get("article_id") or "",
            title=candidate.get("title") or "",
            article_url=candidate["url"],
            apteka_publication_date=candidate.get("card_date"),
            project_reason="TITLE_PROJECT_NOT_OPENED" if candidate.get("title_is_project") else "NOT_PROJECT",
            selection_reason="Не відкривалась детальна сторінка: поза діагностичним періодом",
        ))
    articles.sort(key=lambda item: item.apteka_publication_date or "", reverse=True)
    selected = [item for item in articles if item.selected]

    payload = {
        "generated_at": datetime.now(TZ).isoformat(),
        "configuration": {
            "as_of_date": as_of.isoformat(), "period_start": start.isoformat(),
            "lookback_days": lookback, "max_pages": max_pages,
            "date_basis": basis, "reached_old_period": reached_old,
        },
        "summary": {
            "category_articles": len(candidate_list),
            "project_titles_found": sum(
                1 for item in candidate_list if item.get("title_is_project")
            ),
            "detailed_articles": len(opened),
            "selected": len(selected),
            "selected_with_deadline": sum(1 for item in selected if item.deadline_date),
        },
        "probes": [asdict(item) for item in probes],
        "page_stats": pages,
        "articles": [asdict(item) for item in articles],
    }
    (REPORT_DIR / "apteka_diagnostics.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORT_DIR / "apteka_diagnostics.md").write_text(markdown(probes, articles, as_of, start, basis, pages), encoding="utf-8")
    (REPORT_DIR / "apteka_email_preview.html").write_text(html_report(selected, as_of, start, basis), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))

    category_probe = next((p for p in probes if p.label == "category_moz"), None)
    if not category_probe or category_probe.status != 200:
        print("Категорія МОЗ недоступна", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
