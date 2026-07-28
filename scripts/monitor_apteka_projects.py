from __future__ import annotations

import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
import trafilatura
from bs4 import BeautifulSoup, Tag
from google import genai


BASE_URL = "https://www.apteka.ua"
CATEGORY_URL = f"{BASE_URL}/category/moz"
TIMEZONE = ZoneInfo("Europe/Kyiv")
REQUEST_TIMEOUT = 40
STATE_PATH = Path("data/apteka_projects_state.json")
REPORT_DIR = Path("reports")
MAX_ARTICLE_TEXT_CHARS = 180_000
MAX_PROJECTS_PER_RUN = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.7,en;q=0.6",
    "Cache-Control": "no-cache",
}

MONTHS = {
    "січня": 1,
    "лютого": 2,
    "березня": 3,
    "квітня": 4,
    "травня": 5,
    "червня": 6,
    "липня": 7,
    "серпня": 8,
    "вересня": 9,
    "жовтня": 10,
    "листопада": 11,
    "грудня": 12,
}

UA_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\s*(?:р\.|року)?",
    re.IGNORECASE,
)
DOT_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4})(?!\d)")
ARTICLE_RE = re.compile(
    r"^https?://(?:www\.)?apteka\.ua/article/(\d+)/?$",
    re.IGNORECASE,
)
PROJECT_TITLE_RE = re.compile(r"^\s*(?:проєкт|проект)\b", re.IGNORECASE)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-яІіЇїЄє]{2,}")
PHONE_RE = re.compile(
    r"(?:\+?38[\s\-]?)?\(?0\d{2}\)?[\s\-]?\d{2,3}[\s\-]?\d{2}[\s\-]?\d{2}"
)
FILE_RE = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?|zip)(?:$|[?#])", re.IGNORECASE)

OFFICIAL_DATE_RE = re.compile(
    r"(?:опублікован\w*|оприлюднен\w*).{0,140}?"
    r"(\d{1,2}[./]\d{1,2}[./]\d{4})",
    re.IGNORECASE | re.DOTALL,
)
RANGE_RE = re.compile(
    r"(?:із|з)\s*(\d{1,2}[./]\d{1,2}[./]\d{4})\s*"
    r"(?:р\.?\s*)?(?:по|до|—|-)\s*"
    r"(\d{1,2}[./]\d{1,2}[./]\d{4})",
    re.IGNORECASE,
)
UNTIL_RE = re.compile(
    r"(?:пропозиці\w*|зауважен\w*).{0,300}?\bдо\s+"
    r"(\d{1,2}[./]\d{1,2}[./]\d{4})",
    re.IGNORECASE | re.DOTALL,
)
DURATION_RE = re.compile(
    r"протягом\s+(\d{1,3})\s+(?:календарних\s+)?дн",
    re.IGNORECASE,
)


AI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "act_type": {
            "type": "string",
            "description": "Вид проєкту акта: постанова КМУ, наказ МОЗ, закон тощо.",
        },
        "developer": {
            "type": "string",
            "description": "Орган-розробник проєкту. Порожній рядок, якщо в тексті не встановлено.",
        },
        "official_source_url": {
            "type": "string",
            "description": (
                "Пряме посилання на офіційну державну сторінку оприлюднення "
                "саме цього проєкту. Використовувати лише URL із наданого тексту "
                "або технічних підказок. Порожній рядок, якщо не встановлено."
            ),
        },
        "official_publication_date": {
            "type": "string",
            "description": (
                "Дата офіційного оприлюднення проєкту для обговорення у форматі "
                "YYYY-MM-DD. Не використовувати історичні дати актів із назви. "
                "Порожній рядок, якщо точно не встановлено."
            ),
        },
        "official_date_evidence": {
            "type": "string",
            "description": "Короткий точний фрагмент тексту, що підтверджує офіційну дату.",
        },
        "deadline_date": {
            "type": "string",
            "description": (
                "Останній день подання пропозицій у форматі YYYY-MM-DD. "
                "Порожній рядок, якщо точно не встановлено."
            ),
        },
        "deadline_basis": {
            "type": "string",
            "enum": ["EXPLICIT", "CALCULATED", "NOT_FOUND"],
            "description": (
                "EXPLICIT — кінцева дата прямо наведена; CALCULATED — обчислена "
                "з точної дати початку та тривалості; NOT_FOUND — не встановлена."
            ),
        },
        "deadline_evidence": {
            "type": "string",
            "description": "Короткий точний фрагмент тексту про строк подання.",
        },
        "contact_department": {
            "type": "string",
            "description": "Підрозділ або орган, що приймає пропозиції.",
        },
        "contact_person": {
            "type": "string",
            "description": "ПІБ контактної особи без посади, якщо її зазначено.",
        },
        "contact_position": {
            "type": "string",
            "description": "Посада контактної особи, якщо зазначена.",
        },
        "postal_address": {
            "type": "string",
            "description": "Поштова адреса для подання пропозицій.",
        },
        "emails": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Email-адреси для подання пропозицій. Не вигадувати.",
        },
        "phones": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Телефони для подання пропозицій. Не вигадувати.",
        },
        "submission_format": {
            "type": "string",
            "description": (
                "Вимоги до способу або форми подання: письмово, електронно, "
                "порівняльна таблиця тощо."
            ),
        },
        "contacts_evidence": {
            "type": "string",
            "description": "Короткий точний фрагмент тексту з контактами та формою подання.",
        },
        "summary": {
            "type": "string",
            "description": (
                "Практичне legal summary українською: 2–4 короткі речення, "
                "до 650 символів, без вступних фраз."
            ),
        },
        "practical_impact": {
            "type": "string",
            "description": (
                "До 450 символів: які процеси виробника лікарських засобів, "
                "дистриб'ютора або аптечного бізнесу потенційно зачіпає проєкт."
            ),
        },
        "affected_areas": {
            "type": "array",
            "items": {"type": "string"},
            "description": "До п'яти коротких сфер впливу.",
            "maxItems": 5,
        },
        "confidence": {
            "type": "string",
            "enum": ["HIGH", "MEDIUM", "LOW"],
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Неузгодженості, помилки дат або відсутні дані.",
        },
    },
    "required": [
        "act_type",
        "developer",
        "official_source_url",
        "official_publication_date",
        "official_date_evidence",
        "deadline_date",
        "deadline_basis",
        "deadline_evidence",
        "contact_department",
        "contact_person",
        "contact_position",
        "postal_address",
        "emails",
        "phones",
        "submission_format",
        "contacts_evidence",
        "summary",
        "practical_impact",
        "affected_areas",
        "confidence",
        "warnings",
    ],
    "additionalProperties": False,
}


@dataclass
class Project:
    article_id: str
    title: str
    article_url: str
    apteka_publication_date: str
    article_text: str
    content_hash: str
    document_links: list[str] = field(default_factory=list)
    official_links: list[str] = field(default_factory=list)
    email_hints: list[str] = field(default_factory=list)
    phone_hints: list[str] = field(default_factory=list)
    ai_status: str = ""
    ai_model: str = ""
    ai_cached: bool = False
    act_type: str = ""
    developer: str = ""
    official_source_url: str = ""
    official_publication_date: str = ""
    official_date_source: str = ""
    official_date_evidence: str = ""
    deadline_date: str = ""
    deadline_basis: str = "NOT_FOUND"
    deadline_source: str = ""
    deadline_evidence: str = ""
    contact_department: str = ""
    contact_person: str = ""
    contact_position: str = ""
    postal_address: str = ""
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    submission_format: str = ""
    contacts_evidence: str = ""
    summary: str = ""
    practical_impact: str = ""
    affected_areas: list[str] = field(default_factory=list)
    confidence: str = "LOW"
    warnings: list[str] = field(default_factory=list)
    days_until_deadline: int | None = None


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = clean(value)
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def bool_env(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def parse_iso_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except (TypeError, ValueError):
        return None


def parse_dot_date(value: str | None) -> date | None:
    match = DOT_DATE_RE.search(value or "")
    if not match:
        return None
    day, month, year = map(int, match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_ua_date(value: str | None) -> date | None:
    match = UA_DATE_RE.search(value or "")
    if not match:
        return None
    day = int(match.group(1))
    month = MONTHS[match.group(2).lower()]
    year = int(match.group(3))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def iso(value: date | None) -> str:
    return value.isoformat() if value else ""


def uk_date(value: str | None) -> str:
    parsed = parse_iso_date(value)
    return parsed.strftime("%d.%m.%Y") if parsed else (value or "—")


def article_id(url: str) -> str:
    match = re.search(r"/article/(\d+)", url)
    return match.group(1) if match else ""


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def fetch_html(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "html" not in content_type:
        raise RuntimeError(f"Очікувався HTML, отримано {content_type}: {url}")
    response.encoding = response.encoding or response.apparent_encoding or "utf-8"
    return response.text


def category_date_after_title(link: Tag) -> date | None:
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

        for sibling in list(start_node.next_siblings)[:12]:
            if isinstance(sibling, Tag):
                other = sibling.find("a", href=True)
                if other:
                    other_url = urljoin(BASE_URL, other.get("href", ""))
                    if ARTICLE_RE.match(other_url.rstrip("/") + "/"):
                        break

                time_tag = sibling if sibling.name == "time" else sibling.find("time")
                if isinstance(time_tag, Tag):
                    raw = str(time_tag.get("datetime") or time_tag.get_text(" ", strip=True))
                    parsed = parse_iso_date(raw[:10]) or parse_ua_date(raw)
                    if parsed:
                        return parsed

                value = clean(sibling.get_text(" ", strip=True))
            else:
                value = clean(str(sibling))

            if not value:
                continue

            collected.append(value)
            char_count += len(value) + 1
            parsed = parse_ua_date(" ".join(collected))
            if parsed:
                return parsed

            if char_count >= 700:
                break

    title = clean(link.get_text(" ", strip=True))
    current: Tag | None = heading if isinstance(heading, Tag) else link

    for _ in range(5):
        parent = current.parent if current else None
        if not isinstance(parent, Tag):
            break

        parent_text = clean(parent.get_text(" ", strip=True))
        if len(parent_text) <= 1800:
            without_title = clean(parent_text.replace(title, "", 1))
            parsed = parse_ua_date(without_title)
            if parsed:
                return parsed

        current = parent

    return None


def parse_category(html_text: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html_text, "lxml")
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    for link in soup.find_all("a", href=True):
        absolute = urljoin(BASE_URL, link.get("href", ""))
        canonical = absolute.rstrip("/") + "/"

        if not ARTICLE_RE.match(canonical) or canonical in seen:
            continue

        title = clean(link.get_text(" ", strip=True))
        if not title:
            continue

        published = category_date_after_title(link)

        results.append(
            {
                "article_id": article_id(canonical),
                "title": title,
                "url": canonical,
                "card_date": iso(published),
                "title_is_project": "YES" if PROJECT_TITLE_RE.search(title) else "NO",
            }
        )
        seen.add(canonical)

    return results


def article_publication_date(soup: BeautifulSoup) -> date | None:
    selectors = [
        'meta[property="article:published_time"]',
        'meta[name="article:published_time"]',
        'meta[itemprop="datePublished"]',
        'meta[name="date"]',
    ]

    for selector in selectors:
        tag = soup.select_one(selector)
        if isinstance(tag, Tag):
            raw = str(tag.get("content") or "").strip()
            parsed = parse_iso_date(raw[:10]) or parse_ua_date(raw)
            if parsed:
                return parsed

    for script_tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script_tag.string or script_tag.get_text(" ", strip=True)
        match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', raw or "", re.IGNORECASE)
        if match:
            value = match.group(1)
            parsed = parse_iso_date(value[:10]) or parse_ua_date(value)
            if parsed:
                return parsed

    for time_tag in soup.find_all("time"):
        raw = str(time_tag.get("datetime") or time_tag.get_text(" ", strip=True))
        parsed = parse_iso_date(raw[:10]) or parse_ua_date(raw)
        if parsed:
            return parsed

    h1 = soup.find("h1")
    if not isinstance(h1, Tag):
        return None

    title = clean(h1.get_text(" ", strip=True))
    anchor: Tag | None = h1

    for _ in range(6):
        if not isinstance(anchor, Tag):
            break

        collected: list[str] = []
        for sibling in anchor.next_siblings:
            if isinstance(sibling, Tag):
                if sibling.name in {"h1", "article"}:
                    break
                value = clean(sibling.get_text(" ", strip=True))
            else:
                value = clean(str(sibling))

            if not value:
                continue

            collected.append(value)
            combined = clean(" ".join(collected))
            parsed = parse_ua_date(combined)
            if parsed:
                return parsed

            if len(combined) >= 900:
                break

        anchor = anchor.parent if isinstance(anchor.parent, Tag) else None

    current: Tag | None = h1
    for _ in range(5):
        parent = current.parent if current else None
        if not isinstance(parent, Tag):
            break

        parent_text = clean(parent.get_text(" ", strip=True))
        title_pos = parent_text.find(title)

        if title_pos >= 0:
            after_title = parent_text[title_pos + len(title) : title_pos + len(title) + 900]
            parsed = parse_ua_date(after_title)
            if parsed:
                return parsed

        current = parent

    return None


def extract_article_text(html_text: str, soup: BeautifulSoup) -> str:
    extracted = trafilatura.extract(
        html_text,
        include_comments=False,
        include_tables=True,
        include_links=False,
        favor_recall=True,
    )
    if extracted and len(clean(extracted)) >= 500:
        return clean(extracted)[:MAX_ARTICLE_TEXT_CHARS]

    h1 = soup.find("h1")
    if isinstance(h1, Tag):
        candidates: list[Tag] = []
        current: Tag | None = h1

        for _ in range(7):
            parent = current.parent if current else None
            if not isinstance(parent, Tag):
                break
            candidates.append(parent)
            current = parent

        best_text = ""
        best_score = -1

        for candidate in candidates:
            clone = BeautifulSoup(str(candidate), "lxml")
            for tag in clone.select(
                "script,style,noscript,nav,header,footer,aside,form,.sidebar,.menu,.navigation"
            ):
                tag.decompose()

            text = clean(clone.get_text(" ", strip=True))
            score = len(text)
            if re.search(r"повідомлення\s+про\s+оприлюднення", text, re.IGNORECASE):
                score += 10_000
            if score > best_score:
                best_score = score
                best_text = text

        if best_text:
            return best_text[:MAX_ARTICLE_TEXT_CHARS]

    body = soup.body if isinstance(soup.body, Tag) else soup
    clone = BeautifulSoup(str(body), "lxml")
    for tag in clone.select("script,style,noscript,nav,header,footer,aside,form"):
        tag.decompose()
    return clean(clone.get_text(" ", strip=True))[:MAX_ARTICLE_TEXT_CHARS]


def decode_cfemail(encoded: str) -> str:
    try:
        encoded = encoded.strip()
        key = int(encoded[:2], 16)
        chars = [
            chr(int(encoded[index : index + 2], 16) ^ key)
            for index in range(2, len(encoded), 2)
        ]
        return "".join(chars)
    except (ValueError, IndexError):
        return ""


def extract_email_hints(soup: BeautifulSoup, raw_html: str) -> list[str]:
    found: list[str] = []

    for link in soup.find_all("a", href=True):
        href = html.unescape(str(link.get("href") or ""))
        lower = href.lower()

        if lower.startswith("mailto:"):
            address = unquote(href[7:]).split("?", 1)[0].strip()
            found.extend(part.strip() for part in address.split(",") if part.strip())

        if "/cdn-cgi/l/email-protection#" in lower:
            encoded = href.rsplit("#", 1)[-1]
            decoded = decode_cfemail(encoded)
            if decoded:
                found.append(decoded)

    for tag in soup.find_all(attrs={"data-cfemail": True}):
        decoded = decode_cfemail(str(tag.get("data-cfemail") or ""))
        if decoded:
            found.append(decoded)

    found.extend(EMAIL_RE.findall(html.unescape(raw_html)))

    return unique(
        email.lower()
        for email in found
        if EMAIL_RE.fullmatch(email.strip())
        and email.lower() != "email@example.com"
    )


def extract_phone_hints(text: str) -> list[str]:
    return unique(PHONE_RE.findall(text))


def extract_links(soup: BeautifulSoup) -> tuple[list[str], list[str]]:
    official: list[str] = []
    documents: list[str] = []

    for link in soup.find_all("a", href=True):
        absolute = urljoin(BASE_URL, html.unescape(str(link.get("href") or "")))
        parsed = urlparse(absolute)
        host = parsed.netloc.lower()

        if FILE_RE.search(absolute):
            documents.append(absolute)

        if (
            host.endswith(".gov.ua")
            or host in {"gov.ua", "moz.gov.ua", "www.moz.gov.ua"}
        ) and "apteka.ua" not in host:
            official.append(absolute)

    return unique(official), unique(documents)


def deterministic_official_date(text: str, article_date: date) -> date | None:
    candidates: list[date] = []

    for match in OFFICIAL_DATE_RE.finditer(text):
        parsed = parse_dot_date(match.group(1))
        if parsed:
            candidates.append(parsed)

    for match in RANGE_RE.finditer(text):
        context = text[max(0, match.start() - 300) : match.end() + 150]
        if re.search(r"пропозиці|зауважен|оприлюднен|публікац", context, re.IGNORECASE):
            parsed = parse_dot_date(match.group(1))
            if parsed:
                candidates.append(parsed)

    plausible = [
        candidate
        for candidate in candidates
        if candidate <= article_date + timedelta(days=2)
        and candidate >= article_date - timedelta(days=180)
    ]

    if not plausible:
        return None

    return min(plausible, key=lambda candidate: abs((candidate - article_date).days))


def deterministic_deadline(
    text: str,
    official_date: date | None,
) -> tuple[date | None, str, str]:
    windows: list[str] = []

    for match in re.finditer(r"пропозиці\w*|зауважен\w*", text, re.IGNORECASE):
        windows.append(text[max(0, match.start() - 200) : match.start() + 1200])

    search_text = " ".join(windows[:6]) or text[:5000]

    match = RANGE_RE.search(search_text)
    if match:
        end = parse_dot_date(match.group(2))
        if end:
            return end, "EXPLICIT", clean(
                search_text[max(0, match.start() - 180) : match.end() + 250]
            )

    match = UNTIL_RE.search(search_text)
    if match:
        end = parse_dot_date(match.group(1))
        if end:
            return end, "EXPLICIT", clean(
                search_text[max(0, match.start() - 180) : match.end() + 250]
            )

    match = DURATION_RE.search(search_text)
    if match and official_date:
        duration = int(match.group(1))
        end = official_date + timedelta(days=duration)
        return end, "CALCULATED", clean(
            search_text[max(0, match.start() - 180) : match.end() + 300]
        )

    return None, "NOT_FOUND", ""


def build_ai_prompt(project: Project) -> str:
    hints = {
        "article_title": project.title,
        "apteka_publication_date": project.apteka_publication_date,
        "article_url": project.article_url,
        "emails_found_in_html": project.email_hints,
        "phones_found_in_html": project.phone_hints,
        "official_links": project.official_links[:10],
        "document_links": project.document_links[:20],
    }

    return f"""
Ти аналізуєш публікацію про проєкт нормативно-правового акта у фармацевтичній сфері.

ЗАВДАННЯ:
1. Встанови офіційну дату оприлюднення проєкту для громадського обговорення.
2. Встанови останній день подання пропозицій/зауважень.
3. Витягни точні контакти та вимоги до форми подання.
4. Визнач пряме офіційне державне посилання саме на цей проєкт, якщо воно є.
5. Підготуй коротке практичне legal summary і вплив для виробника лікарських засобів.
6. Поверни лише структурований JSON за заданою схемою.

КРИТИЧНІ ПРАВИЛА:
- Не використовуй як дату оприлюднення дати нормативних актів у назві,
  пояснювальній записці, посиланнях або історичних прикладах.
- Дата публікації Apteka.ua не обов'язково є офіційною датою оприлюднення.
- Якщо в повідомленні наведено період "з DD.MM.YYYY по DD.MM.YYYY",
  початок зазвичай є датою офіційного оприлюднення, а кінець — строком.
- Якщо є лише тривалість у днях і точна дата початку, строк можна обчислити,
  позначивши deadline_basis = CALCULATED.
- Якщо дані не встановлені точно — поверни порожній рядок або порожній масив.
- Не вигадуй email, телефони, ПІБ, дати чи офіційні посилання.
- Evidence-поля мають містити короткі дослівні фрагменти наданого тексту.
- Summary: 2–4 короткі речення, до 650 символів, без вступів і загальної оцінки.
- Practical impact: конкретні процеси компанії, які варто перевірити.

ТЕХНІЧНІ ПІДКАЗКИ З HTML:
{json.dumps(hints, ensure_ascii=False, indent=2)}

ТЕКСТ ПУБЛІКАЦІЇ:
--- BEGIN ARTICLE ---
{project.article_text}
--- END ARTICLE ---
""".strip()


def call_gemini(
    client: genai.Client,
    project: Project,
    model: str,
    max_retries: int,
    delay_seconds: float,
) -> dict[str, Any]:
    prompt = build_ai_prompt(project)
    last_error = ""

    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config={
                    "temperature": 0.1,
                    "max_output_tokens": 1800,
                    "response_mime_type": "application/json",
                    "response_json_schema": AI_SCHEMA,
                },
            )

            raw = (response.text or "").strip()
            if not raw:
                raise RuntimeError("Gemini повернув порожню відповідь")

            result = json.loads(raw)
            if not isinstance(result, dict):
                raise RuntimeError("Gemini повернув не JSON object")

            if delay_seconds > 0:
                time.sleep(delay_seconds)

            return result

        except Exception as exc:  # SDK can raise several transport/API classes.
            last_error = f"{type(exc).__name__}: {exc}"

            if attempt >= max_retries:
                break

            time.sleep(max(delay_seconds, 2.0) * attempt)

    raise RuntimeError(f"Gemini не відповів після {max_retries} спроб: {last_error}")


def valid_date(
    value: str,
    *,
    minimum: date,
    maximum: date,
) -> date | None:
    parsed = parse_iso_date(value)
    if not parsed:
        return None
    if parsed < minimum or parsed > maximum:
        return None
    return parsed


def normalize_phone(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def validate_and_apply_ai(
    project: Project,
    ai: dict[str, Any],
    as_of: date,
) -> None:
    article_date = parse_iso_date(project.apteka_publication_date)
    if not article_date:
        raise RuntimeError("Відсутня валідна дата Apteka.ua")

    project.act_type = clean(str(ai.get("act_type") or ""))
    project.developer = clean(str(ai.get("developer") or ""))

    ai_official_url = clean(str(ai.get("official_source_url") or ""))
    if ai_official_url and ai_official_url in project.official_links:
        project.official_source_url = ai_official_url
    else:
        project.official_source_url = ""

    project.official_date_evidence = clean(str(ai.get("official_date_evidence") or ""))
    project.deadline_evidence = clean(str(ai.get("deadline_evidence") or ""))
    project.contact_department = clean(str(ai.get("contact_department") or ""))
    project.contact_person = clean(str(ai.get("contact_person") or ""))
    project.contact_position = clean(str(ai.get("contact_position") or ""))
    project.postal_address = clean(str(ai.get("postal_address") or ""))
    project.submission_format = clean(str(ai.get("submission_format") or ""))
    project.contacts_evidence = clean(str(ai.get("contacts_evidence") or ""))
    project.summary = clean(str(ai.get("summary") or ""))[:900]
    project.practical_impact = clean(str(ai.get("practical_impact") or ""))[:700]
    project.affected_areas = unique(
        [str(value) for value in (ai.get("affected_areas") or [])]
    )[:5]
    project.confidence = str(ai.get("confidence") or "LOW").upper()
    if project.confidence not in {"HIGH", "MEDIUM", "LOW"}:
        project.confidence = "LOW"

    project.warnings.extend(
        clean(str(value))
        for value in (ai.get("warnings") or [])
        if clean(str(value))
    )

    ai_official = valid_date(
        str(ai.get("official_publication_date") or ""),
        minimum=article_date - timedelta(days=180),
        maximum=min(article_date + timedelta(days=2), as_of + timedelta(days=1)),
    )

    fallback_official = deterministic_official_date(
        project.article_text,
        article_date,
    )

    if ai_official:
        project.official_publication_date = ai_official.isoformat()
        project.official_date_source = "GEMINI_VALIDATED"
    elif fallback_official:
        project.official_publication_date = fallback_official.isoformat()
        project.official_date_source = "REGEX_FALLBACK"
        project.warnings.append(
            "Gemini не встановив валідну офіційну дату; застосовано regex-фолбек."
        )
    else:
        project.official_publication_date = ""
        project.official_date_source = "NOT_FOUND"

    official_date = parse_iso_date(project.official_publication_date)

    ai_deadline = valid_date(
        str(ai.get("deadline_date") or ""),
        minimum=(official_date or article_date),
        maximum=(official_date or article_date) + timedelta(days=180),
    )

    fallback_deadline, fallback_basis, fallback_evidence = deterministic_deadline(
        project.article_text,
        official_date,
    )

    ai_basis = str(ai.get("deadline_basis") or "NOT_FOUND").upper()
    if ai_basis not in {"EXPLICIT", "CALCULATED", "NOT_FOUND"}:
        ai_basis = "NOT_FOUND"

    if ai_deadline:
        project.deadline_date = ai_deadline.isoformat()
        project.deadline_basis = ai_basis if ai_basis != "NOT_FOUND" else "EXPLICIT"
        project.deadline_source = "GEMINI_VALIDATED"
    elif fallback_deadline:
        project.deadline_date = fallback_deadline.isoformat()
        project.deadline_basis = fallback_basis
        project.deadline_source = "REGEX_FALLBACK"
        if not project.deadline_evidence:
            project.deadline_evidence = fallback_evidence
        project.warnings.append(
            "Gemini не встановив валідний строк; застосовано regex-фолбек."
        )
    else:
        project.deadline_date = ""
        project.deadline_basis = "NOT_FOUND"
        project.deadline_source = "NOT_FOUND"

    accepted_emails = list(project.email_hints)
    raw_article_lower = project.article_text.lower()

    for value in ai.get("emails") or []:
        candidate = clean(str(value)).lower()
        if not EMAIL_RE.fullmatch(candidate):
            continue
        if candidate in raw_article_lower or candidate in {
            item.lower() for item in project.email_hints
        }:
            accepted_emails.append(candidate)

    project.emails = unique(accepted_emails)

    accepted_phones = list(project.phone_hints)
    article_digits = normalize_phone(project.article_text)

    for value in ai.get("phones") or []:
        candidate = clean(str(value))
        digits = normalize_phone(candidate)
        if len(digits) >= 9 and digits in article_digits:
            accepted_phones.append(candidate)

    project.phones = unique(accepted_phones)

    deadline = parse_iso_date(project.deadline_date)
    if deadline:
        project.days_until_deadline = (deadline - as_of).days

    if not project.summary:
        project.summary = (
            "Gemini не сформував summary. Перегляньте текст публікації за посиланням."
        )
        project.warnings.append("Порожній AI Summary.")

    project.warnings = unique(project.warnings)


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"version": 1, "updated_at": "", "items": {}}

    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "updated_at": "", "items": {}}

    if not isinstance(data, dict):
        return {"version": 1, "updated_at": "", "items": {}}

    if not isinstance(data.get("items"), dict):
        data["items"] = {}

    return data


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["version"] = 1
    state["updated_at"] = datetime.now(TIMEZONE).isoformat()
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def cache_to_project(project: Project, cache: dict[str, Any]) -> None:
    ai_fields = [
        "ai_status",
        "ai_model",
        "act_type",
        "developer",
        "official_source_url",
        "official_publication_date",
        "official_date_source",
        "official_date_evidence",
        "deadline_date",
        "deadline_basis",
        "deadline_source",
        "deadline_evidence",
        "contact_department",
        "contact_person",
        "contact_position",
        "postal_address",
        "emails",
        "phones",
        "submission_format",
        "contacts_evidence",
        "summary",
        "practical_impact",
        "affected_areas",
        "confidence",
        "warnings",
        "days_until_deadline",
    ]

    for field_name in ai_fields:
        if field_name in cache:
            setattr(project, field_name, cache[field_name])

    project.ai_cached = True


def project_to_cache(project: Project) -> dict[str, Any]:
    payload = asdict(project)
    payload.pop("article_text", None)
    payload["cached_at"] = datetime.now(TIMEZONE).isoformat()
    return payload


def scan_projects(
    session: requests.Session,
    start: date,
    as_of: date,
    max_pages: int,
) -> tuple[list[Project], list[dict[str, Any]]]:
    candidates: dict[str, dict[str, str]] = {}
    page_stats: list[dict[str, Any]] = []

    for page_number in range(1, max_pages + 1):
        page_url = (
            CATEGORY_URL
            if page_number == 1
            else f"{CATEGORY_URL}/page/{page_number}/"
        )
        html_text = fetch_html(session, page_url)
        parsed = parse_category(html_text)

        dates = sorted(
            parsed_date
            for item in parsed
            if (parsed_date := parse_iso_date(item.get("card_date")))
        )

        page_stats.append(
            {
                "page": page_number,
                "url": page_url,
                "articles": len(parsed),
                "project_titles": sum(
                    item.get("title_is_project") == "YES" for item in parsed
                ),
                "newest_date": iso(dates[-1]) if dates else "",
                "oldest_date": iso(dates[0]) if dates else "",
            }
        )

        for item in parsed:
            candidates.setdefault(item["url"], item)

        if not parsed:
            break

        if dates and dates[-1] < start:
            break

        if dates and dates[0] < start and page_number >= 1:
            break

    project_candidates = [
        item
        for item in candidates.values()
        if item.get("title_is_project") == "YES"
    ][:MAX_PROJECTS_PER_RUN]

    projects: list[Project] = []

    for candidate in project_candidates:
        html_text = fetch_html(session, candidate["url"])
        soup = BeautifulSoup(html_text, "lxml")

        h1 = soup.find("h1")
        title = (
            clean(h1.get_text(" ", strip=True))
            if isinstance(h1, Tag)
            else candidate["title"]
        )

        publication_date = article_publication_date(soup)
        if not publication_date:
            publication_date = parse_iso_date(candidate.get("card_date"))

        if not publication_date or not (start <= publication_date <= as_of):
            continue

        article_text = extract_article_text(html_text, soup)
        if not article_text:
            continue

        official_links, document_links = extract_links(soup)
        email_hints = extract_email_hints(soup, html_text)
        phone_hints = extract_phone_hints(article_text)

        content_hash = hashlib.sha256(
            (
                title
                + "\n"
                + publication_date.isoformat()
                + "\n"
                + article_text
                + "\n"
                + "\n".join(document_links)
            ).encode("utf-8")
        ).hexdigest()

        projects.append(
            Project(
                article_id=candidate["article_id"],
                title=title,
                article_url=candidate["url"],
                apteka_publication_date=publication_date.isoformat(),
                article_text=article_text,
                content_hash=content_hash,
                document_links=document_links,
                official_links=official_links,
                email_hints=email_hints,
                phone_hints=phone_hints,
            )
        )

        time.sleep(0.25)

    projects.sort(
        key=lambda project: (
            project.apteka_publication_date,
            project.article_id,
        ),
        reverse=True,
    )

    return projects, page_stats


def deadline_html(project: Project) -> str:
    if not project.deadline_date:
        return '<span style="color:#9a3412;font-weight:700;">не визначено</span>'

    days = project.days_until_deadline

    if days is None:
        color = "#333333"
        label = uk_date(project.deadline_date)
    elif days < 0:
        color = "#6b7280"
        label = f"{uk_date(project.deadline_date)} — строк минув"
    elif days <= 3:
        color = "#b91c1c"
        label = f"{uk_date(project.deadline_date)} — залишилось {days} дн."
    elif days <= 7:
        color = "#b45309"
        label = f"{uk_date(project.deadline_date)} — залишилось {days} дн."
    else:
        color = "#166534"
        label = f"{uk_date(project.deadline_date)} — залишилось {days} дн."

    return (
        f'<span style="color:{color};font-weight:700;">'
        f"{html.escape(label)}</span>"
    )


def escape(value: object) -> str:
    return html.escape(str(value or "—"), quote=True)


def render_contacts(project: Project) -> str:
    lines: list[str] = []

    if project.contact_department:
        lines.append(
            f"<div><strong>Підрозділ:</strong> {escape(project.contact_department)}</div>"
        )

    person = " — ".join(
        value
        for value in [project.contact_person, project.contact_position]
        if value
    )
    if person:
        lines.append(f"<div><strong>Контакт:</strong> {escape(person)}</div>")

    if project.postal_address:
        lines.append(
            f"<div><strong>Адреса:</strong> {escape(project.postal_address)}</div>"
        )

    if project.emails:
        lines.append(
            f"<div><strong>Email:</strong> {escape(', '.join(project.emails))}</div>"
        )

    if project.phones:
        lines.append(
            f"<div><strong>Телефон:</strong> {escape(', '.join(project.phones))}</div>"
        )

    if project.submission_format:
        lines.append(
            f'<div style="margin-top:6px;"><strong>Форма:</strong> '
            f"{escape(project.submission_format)}</div>"
        )

    return "".join(lines) or "—"


def render_links(project: Project) -> str:
    links = [
        f'<div><a href="{escape(project.article_url)}">Публікація Apteka.ua</a></div>'
    ]

    if project.official_source_url:
        links.append(
            f'<div><a href="{escape(project.official_source_url)}">'
            "Офіційне джерело</a></div>"
        )

    for index, url in enumerate(project.document_links[:5], start=1):
        label = "Документ" if len(project.document_links) == 1 else f"Документ {index}"
        links.append(f'<div><a href="{escape(url)}">{label}</a></div>')

    if len(project.document_links) > 5:
        links.append(
            f'<div style="font-size:12px;color:#666;">'
            f"Ще документів: {len(project.document_links) - 5}</div>"
        )

    return "".join(links)


def build_html_report(
    projects: list[Project],
    start: date,
    as_of: date,
) -> str:
    rows: list[str] = []

    for project in projects:
        areas = ""
        if project.affected_areas:
            areas = (
                '<div style="margin-top:8px;font-size:12px;color:#1d4ed8;">'
                + escape(" • ".join(project.affected_areas))
                + "</div>"
            )

        warnings = ""
        if project.warnings:
            warnings = (
                '<div style="margin-top:8px;font-size:12px;color:#9a3412;">'
                + escape("; ".join(project.warnings[:3]))
                + "</div>"
            )

        source_note = (
            '<div style="margin-top:6px;font-size:11px;color:#777;">'
            f"AI: {escape(project.ai_model)}"
            + (" · кеш" if project.ai_cached else "")
            + "</div>"
        )

        rows.append(
            f"""
<tr>
  <td style="border:1px solid #d9d9d9;padding:10px;vertical-align:top;">
    <div><strong>Apteka.ua:</strong> {escape(uk_date(project.apteka_publication_date))}</div>
    <div><strong>Офіційно:</strong> {escape(uk_date(project.official_publication_date))}</div>
    <div style="margin-top:8px;"><strong>Пропозиції до:</strong><br>{deadline_html(project)}</div>
  </td>
  <td style="border:1px solid #d9d9d9;padding:10px;vertical-align:top;">
    <div style="font-weight:600;">{escape(project.title)}</div>
    <div style="margin-top:8px;color:#555;">
      <strong>Розробник:</strong> {escape(project.developer)}
    </div>
    <div style="font-size:12px;color:#666;">
      {escape(project.act_type)}
    </div>
    {warnings}
  </td>
  <td style="border:1px solid #d9d9d9;padding:10px;vertical-align:top;">
    <div>{escape(project.summary)}</div>
    <div style="margin-top:8px;">
      <strong>Практичний вплив:</strong> {escape(project.practical_impact)}
    </div>
    {areas}
    {source_note}
  </td>
  <td style="border:1px solid #d9d9d9;padding:10px;vertical-align:top;">
    {render_contacts(project)}
  </td>
  <td style="border:1px solid #d9d9d9;padding:10px;vertical-align:top;line-height:1.8;">
    {render_links(project)}
  </td>
</tr>
"""
        )

    body = "".join(rows) or (
        '<tr><td colspan="5" style="border:1px solid #d9d9d9;padding:12px;">'
        "Проєктів за період не знайдено.</td></tr>"
    )

    return f"""<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <title>Дайджест проєктів НПА у фармі</title>
</head>
<body>
<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.45;color:#222;">
  <h1 style="font-size:20px;margin:0 0 12px 0;">
    Моніторинг проєктів НПА у фармі
  </h1>

  <p style="margin:0 0 8px 0;">
    Період:
    <strong>{start.strftime("%d.%m.%Y")}</strong> –
    <strong>{as_of.strftime("%d.%m.%Y")}</strong>
  </p>

  <p style="margin:0 0 16px 0;">
    Усього відібрано проєктів: <strong>{len(projects)}</strong>.
  </p>

  <table style="width:100%;border-collapse:collapse;table-layout:fixed;font-family:Arial,sans-serif;font-size:14px;">
    <colgroup>
      <col style="width:17%;">
      <col style="width:24%;">
      <col style="width:30%;">
      <col style="width:19%;">
      <col style="width:10%;">
    </colgroup>
    <thead>
      <tr>
        <th style="text-align:left;border:1px solid #d9d9d9;background:#f3f4f6;padding:10px;vertical-align:top;">
          Оприлюднення і строк
        </th>
        <th style="text-align:left;border:1px solid #d9d9d9;background:#f3f4f6;padding:10px;vertical-align:top;">
          Назва проєкту
        </th>
        <th style="text-align:left;border:1px solid #d9d9d9;background:#f3f4f6;padding:10px;vertical-align:top;">
          AI Summary / практичний вплив
        </th>
        <th style="text-align:left;border:1px solid #d9d9d9;background:#f3f4f6;padding:10px;vertical-align:top;">
          Подання пропозицій
        </th>
        <th style="text-align:left;border:1px solid #d9d9d9;background:#f3f4f6;padding:10px;vertical-align:top;">
          Джерела
        </th>
      </tr>
    </thead>
    <tbody>
      {body}
    </tbody>
  </table>

  <p style="font-size:12px;color:#666;margin:16px 0 0 0;">
    Джерело виявлення: Apteka.ua. Дані AI перевіряються програмними правилами;
    за відсутності валідної відповіді застосовується фолбек.
  </p>
  <p style="font-size:12px;color:#666;margin:4px 0 0 0;">
    Звіт сформовано:
    {datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M:%S")}
  </p>
</div>
</body>
</html>
"""


def build_markdown_report(
    projects: list[Project],
    start: date,
    as_of: date,
    page_stats: list[dict[str, Any]],
) -> str:
    lines = [
        "# Моніторинг проєктів НПА у фармі",
        "",
        f"- Період: **{start.strftime('%d.%m.%Y')}–{as_of.strftime('%d.%m.%Y')}**",
        f"- Відібрано проєктів: **{len(projects)}**",
        f"- Сторінок категорії перевірено: **{len(page_stats)}**",
        f"- AI із кешу: **{sum(project.ai_cached for project in projects)}**",
        f"- Нових AI-запитів: **{sum(not project.ai_cached for project in projects)}**",
        "",
        "## Проєкти",
        "",
        "| Apteka.ua | Офіційно | Строк | Назва | AI |",
        "|---|---|---|---|---|",
    ]

    for project in projects:
        lines.append(
            f"| {uk_date(project.apteka_publication_date)} | "
            f"{uk_date(project.official_publication_date)} | "
            f"{uk_date(project.deadline_date)} | "
            f"[{project.title.replace('|', ' ')}]({project.article_url}) | "
            f"{project.ai_status}{' / cache' if project.ai_cached else ''} |"
        )

    lines.extend(
        [
            "",
            "## Сторінки категорії",
            "",
            "| Сторінка | Статей | Проєктів у назвах | Найновіша | Найстаріша |",
            "|---:|---:|---:|---|---|",
        ]
    )

    for stat in page_stats:
        lines.append(
            f"| {stat['page']} | {stat['articles']} | {stat['project_titles']} | "
            f"{stat['newest_date'] or '—'} | {stat['oldest_date'] or '—'} |"
        )

    lines.append("")
    return "\n".join(lines)


def split_recipients(value: str) -> list[str]:
    return unique(
        part.strip()
        for part in re.split(r"[,;]", value or "")
        if part.strip()
    )


def send_email(html_body: str, start: date, as_of: date) -> None:
    host = (os.getenv("SMTP_HOST") or "smtp.gmail.com").strip()
    port = int((os.getenv("SMTP_PORT") or "465").strip())
    username = (os.getenv("SMTP_USERNAME") or "").strip()
    password = (os.getenv("SMTP_PASSWORD") or "").strip()
    recipients = split_recipients(os.getenv("EMAIL_TO") or "")
    from_name = (
        os.getenv("EMAIL_FROM_NAME") or "Дайджест проєктів НПА у фармі"
    ).strip()
    subject_prefix = (
        os.getenv("EMAIL_SUBJECT_PREFIX") or "Дайджест проєктів НПА у фармі"
    ).strip()

    missing = []
    if not username:
        missing.append("SMTP_USERNAME")
    if not password:
        missing.append("SMTP_PASSWORD")
    if not recipients:
        missing.append("EMAIL_TO")

    if missing:
        raise RuntimeError(
            "Для надсилання email не задано: " + ", ".join(missing)
        )

    subject = (
        f"{subject_prefix} "
        f"[{start.strftime('%d.%m.%Y')} — {as_of.strftime('%d.%m.%Y')}]"
    )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((from_name, username))
    message["To"] = ", ".join(recipients)
    message.set_content(
        "Цей лист містить HTML-звіт. Відкрийте його у поштовому клієнті з підтримкою HTML."
    )
    message.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()

    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=40) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=40) as smtp:
            smtp.starttls(context=context)
            smtp.login(username, password)
            smtp.send_message(message)


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    as_of_raw = (os.getenv("AS_OF_DATE") or "").strip()
    as_of = (
        parse_iso_date(as_of_raw)
        if as_of_raw
        else datetime.now(TIMEZONE).date()
    )
    if not as_of:
        raise ValueError("AS_OF_DATE має бути у форматі YYYY-MM-DD")

    lookback_days = int_env("LOOKBACK_DAYS", 14, 1, 90)
    max_pages = int_env("MAX_PAGES", 5, 1, 30)
    force_ai = bool_env("FORCE_AI", False)
    send_email_enabled = bool_env("SEND_EMAIL", False)
    model = (os.getenv("GEMINI_MODEL") or "gemini-3.1-flash-lite").strip()
    max_retries = int_env("GEMINI_MAX_RETRIES", 3, 1, 6)
    delay_seconds = float((os.getenv("GEMINI_DELAY_SECONDS") or "4").strip())

    start = as_of - timedelta(days=max(lookback_days - 1, 0))

    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("Не задано GitHub Secret GEMINI_API_KEY")

    session = make_session()
    projects, page_stats = scan_projects(
        session,
        start=start,
        as_of=as_of,
        max_pages=max_pages,
    )

    state = load_state()
    state_items = state.setdefault("items", {})
    client = genai.Client(api_key=api_key)

    for project in projects:
        old = state_items.get(project.article_id, {})

        can_reuse = (
            not force_ai
            and old.get("content_hash") == project.content_hash
            and old.get("ai_model") == model
            and old.get("ai_status") == "OK"
        )

        if can_reuse:
            cache_to_project(project, old)
            project.ai_status = "OK"
            project.ai_model = model

            cached_deadline = parse_iso_date(project.deadline_date)
            project.days_until_deadline = (
                (cached_deadline - as_of).days if cached_deadline else None
            )
        else:
            try:
                ai_result = call_gemini(
                    client,
                    project,
                    model=model,
                    max_retries=max_retries,
                    delay_seconds=delay_seconds,
                )
                validate_and_apply_ai(project, ai_result, as_of)
                project.ai_status = "OK"
                project.ai_model = model
            except Exception as exc:
                project.ai_status = "ERROR"
                project.ai_model = model
                project.warnings.append(f"Gemini error: {type(exc).__name__}: {exc}")

                article_date = parse_iso_date(project.apteka_publication_date)
                if article_date:
                    fallback_official = deterministic_official_date(
                        project.article_text,
                        article_date,
                    )
                    project.official_publication_date = iso(fallback_official)
                    project.official_date_source = (
                        "REGEX_FALLBACK" if fallback_official else "NOT_FOUND"
                    )

                    fallback_deadline, fallback_basis, fallback_evidence = (
                        deterministic_deadline(
                            project.article_text,
                            fallback_official,
                        )
                    )
                    project.deadline_date = iso(fallback_deadline)
                    project.deadline_basis = fallback_basis
                    project.deadline_source = (
                        "REGEX_FALLBACK" if fallback_deadline else "NOT_FOUND"
                    )
                    project.deadline_evidence = fallback_evidence
                    project.emails = project.email_hints
                    project.phones = project.phone_hints

                    if fallback_deadline:
                        project.days_until_deadline = (
                            fallback_deadline - as_of
                        ).days

                project.summary = (
                    "AI Summary тимчасово не сформовано через помилку Gemini."
                )
                project.practical_impact = (
                    "Перегляньте публікацію та документи за посиланнями."
                )

        state_items[project.article_id] = project_to_cache(project)

    save_state(state)

    html_report = build_html_report(projects, start, as_of)
    json_report = {
        "generated_at": datetime.now(TIMEZONE).isoformat(),
        "period": {
            "start": start.isoformat(),
            "end": as_of.isoformat(),
            "lookback_days": lookback_days,
        },
        "model": model,
        "summary": {
            "selected": len(projects),
            "ai_cached": sum(project.ai_cached for project in projects),
            "ai_new": sum(not project.ai_cached for project in projects),
            "ai_errors": sum(project.ai_status != "OK" for project in projects),
            "with_official_date": sum(
                bool(project.official_publication_date) for project in projects
            ),
            "with_deadline": sum(bool(project.deadline_date) for project in projects),
            "with_contacts": sum(
                bool(
                    project.contact_person
                    or project.emails
                    or project.phones
                    or project.postal_address
                )
                for project in projects
            ),
        },
        "page_stats": page_stats,
        "projects": [
            {
                key: value
                for key, value in asdict(project).items()
                if key != "article_text"
            }
            for project in projects
        ],
    }

    markdown_report = build_markdown_report(
        projects,
        start,
        as_of,
        page_stats,
    )

    (REPORT_DIR / "latest_apteka_projects.html").write_text(
        html_report,
        encoding="utf-8",
    )
    (REPORT_DIR / "latest_apteka_projects.json").write_text(
        json.dumps(json_report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (REPORT_DIR / "latest_apteka_projects.md").write_text(
        markdown_report,
        encoding="utf-8",
    )
    (REPORT_DIR / "monitor_run.json").write_text(
        json.dumps(
            {
                "ok": True,
                "generated_at": datetime.now(TIMEZONE).isoformat(),
                "send_email_requested": send_email_enabled,
                **json_report["summary"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(json.dumps(json_report["summary"], ensure_ascii=False, indent=2))

    if send_email_enabled and projects:
        send_email(html_report, start, as_of)
        print("Email надіслано.")
    elif send_email_enabled:
        print("Проєктів немає — email не надсилався.")
    else:
        print("SEND_EMAIL=false — створено лише звіт.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / "monitor_run.json").write_text(
            json.dumps(
                {
                    "ok": False,
                    "generated_at": datetime.now(TIMEZONE).isoformat(),
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
