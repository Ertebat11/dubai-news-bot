#!/usr/bin/env python3
import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
import feedparser
import requests
import yaml
from dateutil import parser as date_parser


DEFAULT_CONFIG = "feeds.yaml"
DEFAULT_DB = "seen.sqlite3"
TELEGRAM_TEXT_LIMIT = 3900


@dataclass
class Story:
    source: str
    title: str
    link: str
    summary: str
    published: datetime
    score: int
    reasons: list[str]
    image_url: str = ""

    @property
    def key(self) -> str:
        raw = self.link or f"{self.source}:{self.title}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class StoryCluster:
    key: str
    title: str
    stories: list[Story]
    score: int
    reasons: list[str]
    tags: list[str]

    @property
    def sources(self) -> list[str]:
        return sorted({story.source for story in self.stories})

    @property
    def links(self) -> list[str]:
        seen_links = set()
        links = []
        for story in self.stories:
            if story.link not in seen_links:
                links.append(story.link)
                seen_links.add(story.link)
        return links

    @property
    def best_story(self) -> Story:
        return max(self.stories, key=lambda story: (story.score, story.published))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: Any) -> datetime:
    if not value:
        return utcnow()
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        timestamp = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    try:
        parsed = parsedate_to_datetime(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        parsed = date_parser.parse(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return utcnow()


def date_from_url(url: str) -> datetime | None:
    match = re.search(r"/(20\d{2})/([01]\d)/([0-3]\d)/", url)
    if match:
        try:
            year, month, day = (int(part) for part in match.groups())
            return datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            return None
    match = re.search(
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*-(\d{1,2})-(20\d{2})",
        url,
        re.I,
    )
    if not match:
        return None
    months = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    try:
        return datetime(int(match.group(3)), months[match.group(1).lower()[:3]], int(match.group(2)), tzinfo=timezone.utc)
    except ValueError:
        return None


def clean_text(value: str, limit: int = 220) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"^[A-Z][a-z]{2,8}\s+\d{1,2},\s+20\d{2}\s+", "", value)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "..."


def clean_image_url(value: Any, base_url: str = "") -> str:
    if isinstance(value, dict):
        for key in ("url", "src", "href"):
            found = clean_image_url(value.get(key), base_url)
            if found:
                return found
        return ""
    if isinstance(value, list):
        for item in value:
            found = clean_image_url(item, base_url)
            if found:
                return found
        return ""
    if not value:
        return ""
    url = html.unescape(str(value))
    if not re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", url, re.I):
        return ""
    return urljoin(base_url, url)


def find_image_url(value: Any, base_url: str = "") -> str:
    if isinstance(value, dict):
        for key in ("image", "images", "image-url", "imageUrl", "thumbnailUrl", "hero-image", "heroImage", "media"):
            found = clean_image_url(value.get(key), base_url)
            if found:
                return found
        for child in value.values():
            found = find_image_url(child, base_url)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_image_url(child, base_url)
            if found:
                return found
    return ""


def rss_image_url(entry: dict[str, Any]) -> str:
    for key in ("media_content", "media_thumbnail", "enclosures", "links"):
        found = clean_image_url(entry.get(key))
        if found:
            return found
    return ""


def load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_items (
            key TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            sent_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_groups (
            key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            sources TEXT NOT NULL,
            sent_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_key TEXT NOT NULL,
            action TEXT NOT NULL,
            user_id TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS saved_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            note TEXT,
            user_id TEXT,
            chat_id TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_rewrites (
            group_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watch_terms (
            term TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_key TEXT NOT NULL,
            action TEXT NOT NULL,
            user_id TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    return conn


def seen_story(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute("SELECT 1 FROM sent_items WHERE key = ?", (key,)).fetchone()
    return row is not None


def seen_cluster(conn: sqlite3.Connection, cluster: StoryCluster) -> bool:
    row = conn.execute("SELECT 1 FROM sent_groups WHERE key = ?", (cluster.key,)).fetchone()
    if row:
        return True
    return all(seen_story(conn, story.key) for story in cluster.stories)


def mark_seen(conn: sqlite3.Connection, cluster: StoryCluster) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sent_groups (key, title, sources, sent_at) VALUES (?, ?, ?, ?)",
        (cluster.key, cluster.title, ", ".join(cluster.sources), utcnow().isoformat()),
    )
    for story in cluster.stories:
        conn.execute(
            "INSERT OR IGNORE INTO sent_items (key, source, title, link, sent_at) VALUES (?, ?, ?, ?, ?)",
            (story.key, story.source, story.title, story.link, utcnow().isoformat()),
        )
    conn.commit()


def save_feedback(conn: sqlite3.Connection, group_key: str, action: str, user_id: str | None) -> None:
    conn.execute(
        "INSERT INTO feedback (group_key, action, user_id, created_at) VALUES (?, ?, ?, ?)",
        (group_key, action, user_id, utcnow().isoformat()),
    )
    conn.commit()


def save_approval(conn: sqlite3.Connection, group_key: str, action: str, user_id: str | None) -> None:
    conn.execute(
        "INSERT INTO approvals (group_key, action, user_id, created_at) VALUES (?, ?, ?, ?)",
        (group_key, action, user_id, utcnow().isoformat()),
    )
    conn.commit()


def list_watch_terms(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT term FROM watch_terms ORDER BY term").fetchall()
    return [row[0] for row in rows]


def add_watch_term(conn: sqlite3.Connection, term: str) -> None:
    term = clean_text(term.lower(), 80)
    if term:
        conn.execute("INSERT OR IGNORE INTO watch_terms (term, created_at) VALUES (?, ?)", (term, utcnow().isoformat()))
        conn.commit()


def remove_watch_term(conn: sqlite3.Connection, term: str) -> int:
    term = clean_text(term.lower(), 80)
    cur = conn.execute("DELETE FROM watch_terms WHERE term = ?", (term,))
    conn.commit()
    return cur.rowcount


def state_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def state_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def keyword_score(text: str, keywords: list[dict[str, Any]]) -> tuple[int, list[str]]:
    lower = text.lower()
    score = 0
    reasons: list[str] = []
    for item in keywords:
        terms = item.get("terms", [])
        weight = int(item.get("weight", 1))
        label = item.get("label") or ", ".join(terms[:2])
        if any(term.lower() in lower for term in terms):
            score += weight
            reasons.append(str(label))
    return score, reasons


STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "into",
    "after",
    "over",
    "during",
    "ahead",
    "uae",
    "dubai",
    "abu",
    "dhabi",
    "eid",
    "adha",
    "says",
    "watch",
    "video",
    "news",
    "latest",
}


CONCEPT_PATTERNS = [
    ("uae", r"\buae\b|emirates|الإمارات|الامارات|دولة الإمارات"),
    ("dubai", r"\bdubai\b|دبي"),
    ("abu-dhabi", r"abu dhabi|أبوظبي|ابوظبي"),
    ("sharjah", r"sharjah|الشارقة"),
    ("ajman", r"ajman|عجمان"),
    ("police", r"police|شرطة|مركز شرطة"),
    ("court", r"court|legal|محكمة|قضية"),
    ("crime", r"crime|arrest|robbed|scam|fraud|knife|smuggling|cocaine|سرقة|احتيال|مخدرات|قبض|مشاجرة"),
    ("fire", r"fire|حريق"),
    ("crash", r"crash|collision|accident|حادث|تصادم"),
    ("weather", r"weather|heat|temperature|rain|dust|طقس|حرارة|غبار|أمطار"),
    ("traffic", r"traffic|road|parking|metro|salik|rta|ازدحام|مواقف|مترو|سالك"),
    ("visa", r"visa|waiver|immigration|تأشيرة|اعفاء|الإعفاء"),
    ("agreement", r"agreement|memorandum|mou|deal|مذكرة|تفاهم|اتفاق"),
    ("eswatini", r"eswatini|إسواتيني|اسواتيني"),
    ("kenya", r"kenya|كينيا"),
    ("kuwait", r"kuwait|كويت|الكويت"),
    ("belgium", r"belgium|بلجيكا"),
    ("school", r"school|student|students|مدرسة|طالب|طلاب|طالبات"),
    ("girls", r"girls|بنات|طالبات"),
    ("solidarity", r"solidarity|condolences|condemns|تعزي|تتضامن|يدين|إدانة"),
    ("indian", r"indian|هندي|هندياً|الهند"),
    ("returned", r"return|returned|handing|found|honou?red|عثر|سلم|سلّم|يكرم|كرم"),
    ("aed", r"\baed\b|\bdh\b|dirham|درهم"),
    ("100k", r"100,?000|100 ألف|١٠٠ ألف"),
    ("lottery", r"lottery|يانصيب|لوتري"),
    ("eid", r"eid|عيد"),
    ("viral", r"viral|trending|watch|video|فيديو|ترند"),
    ("lifestyle", r"restaurant|brunch|hotel|mall|pop-up|popup|concert|festival|karak|مطعم|فندق|مول|فعالية|مهرجان"),
    ("business", r"startup|investment|property|real estate|business|economy|استثمار|عقار|اقتصاد"),
]


GENERIC_CONCEPTS = {
    "concept:uae",
    "concept:dubai",
    "concept:police",
    "concept:crime",
    "concept:school",
    "concept:solidarity",
    "concept:lifestyle",
    "concept:business",
    "concept:traffic",
}


DISTINCTIVE_CONCEPTS = {
    "concept:eswatini",
    "concept:kenya",
    "concept:kuwait",
    "concept:belgium",
    "concept:indian",
    "concept:returned",
    "concept:100k",
    "concept:lottery",
    "concept:weather",
    "concept:visa",
    "concept:fire",
    "concept:crash",
}


IDENTITY_CONCEPTS = {
    "concept:eswatini",
    "concept:kenya",
    "concept:kuwait",
    "concept:belgium",
    "concept:indian",
    "concept:100k",
    "concept:lottery",
    "concept:visa",
}


def normalized_tokens(text: str) -> set[str]:
    text = clean_text(text, 500).lower()
    text = re.sub(r"https?://\S+", " ", text)
    raw = re.sub(r"[^\w\s\u0600-\u06FF]", " ", text)
    tokens = {token for token in raw.split() if len(token) > 2 and token not in STOPWORDS}
    for match in re.findall(r"\d+(?:,\d+)?", text):
        tokens.add(match.replace(",", ""))
    for concept, pattern in CONCEPT_PATTERNS:
        if re.search(pattern, text, re.I):
            tokens.add(f"concept:{concept}")
    return tokens


def cluster_similarity(left: StoryCluster, story: Story) -> float:
    left_text = " ".join([left.title, *(item.summary for item in left.stories[:3])])
    right_text = f"{story.title} {story.summary}"
    left_tokens = normalized_tokens(left_text)
    right_tokens = normalized_tokens(right_text)
    if not left_tokens or not right_tokens:
        return 0
    left_words = {token for token in left_tokens if not token.startswith("concept:")}
    right_words = {token for token in right_tokens if not token.startswith("concept:")}
    if left_words and right_words:
        similarity = len(left_words & right_words) / min(len(left_words), len(right_words))
    else:
        similarity = 0
    shared_concepts = {token for token in left_tokens & right_tokens if token.startswith("concept:")}
    distinctive_overlap = len(shared_concepts & DISTINCTIVE_CONCEPTS)
    identity_overlap = len(shared_concepts & IDENTITY_CONCEPTS)
    strong_fact_overlap = distinctive_overlap >= 2 or (
        identity_overlap >= 1 and len(shared_concepts - GENERIC_CONCEPTS) >= 2
    )
    if strong_fact_overlap:
        similarity = max(similarity, 0.6)
    return similarity


def classify_story(story: Story) -> list[str]:
    text = f"{story.title} {story.summary}".lower()
    tags = []
    checks = [
        ("crime", r"crime|arrest|police|court|robbed|scam|fraud|knife|smuggling|cocaine|شرطة|محكمة|سرقة|احتيال|مخدرات|قبض|مشاجرة"),
        ("breaking", r"breaking|urgent|alert|fire|crash|accident|arrest|police|court|crime|عاجل|شرطة|حادث|حريق"),
        ("viral", r"viral|trending|watch|video|influencer|tiktok|instagram|فيديو|ترند"),
        ("lifestyle", r"restaurant|brunch|hotel|mall|pop-up|popup|concert|festival|weekend|eid|karak|cafe|caf[eé]|steakhouse|pool|bar|nightlife|فعالية|مهرجان"),
        ("rules", r"\b(?:visa|fine|law|rule|permit|salik|parking|metro|rta)\b|تأشيرة|غرامة"),
        ("weather/traffic", r"weather|traffic|rain|heat|dust|road|parking|طقس|ازدحام"),
        ("business", r"startup|investment|property|real estate|business|deal|profit|economy|استثمار|اقتصاد"),
    ]
    for label, pattern in checks:
        if re.search(pattern, text, re.I):
            tags.append(label)
    return tags or ["general"]


DIGEST_ALIASES = {
    "all": None,
    "top": None,
    "latest": None,
    "lifestyle": "lifestyle",
    "life": "lifestyle",
    "events": "lifestyle",
    "viral": "viral",
    "social": "viral",
    "crime": "crime",
    "court": "crime",
    "police": "crime",
    "breaking": "breaking",
    "rules": "rules",
    "visa": "rules",
    "weather": "weather/traffic",
    "traffic": "weather/traffic",
    "business": "business",
    "realestate": "business",
    "property": "business",
}


def digest_category_from_text(text: str) -> str | None:
    parts = text.split()
    if len(parts) < 2:
        return None
    raw = parts[1].split("@", 1)[0].strip().lower().replace("-", "").replace("_", "")
    return DIGEST_ALIASES.get(raw, raw)


def filter_clusters_by_category(clusters: list[StoryCluster], category: str | None) -> list[StoryCluster]:
    if not category:
        return clusters
    return [cluster for cluster in clusters if category in cluster.tags]


def build_digest_clusters(config: dict[str, Any], hours: int, min_score: int, limit: int, category: str | None) -> list[StoryCluster]:
    candidates = [story for story in collect(config, hours) if story.score >= min_score]
    clusters = build_clusters(candidates)
    return filter_clusters_by_category(clusters, category)[:limit]


def cluster_image_url(cluster: StoryCluster) -> str:
    for story in sorted(cluster.stories, key=lambda item: item.score, reverse=True):
        if story.image_url:
            return story.image_url
    return ""


def apply_watch_boost(clusters: list[StoryCluster], terms: list[str]) -> list[StoryCluster]:
    if not terms:
        return clusters
    for cluster in clusters:
        text = " ".join([cluster.title, *(story.summary for story in cluster.stories)]).lower()
        matches = [term for term in terms if term.lower() in text]
        if matches:
            cluster.score += min(8, len(matches) * 4)
            reason = "watchlist: " + ", ".join(matches[:3])
            if reason not in cluster.reasons:
                cluster.reasons.insert(0, reason)
    return sorted(clusters, key=lambda item: (item.score, item.best_story.published), reverse=True)


def caption_summary(cluster: StoryCluster) -> str:
    best = cluster.best_story
    base = best.summary or cluster.title
    base = clean_text(base, 260)
    if base == cluster.title and len(cluster.sources) > 1:
        base = f"{cluster.title}. چند منبع اماراتی همزمان این خبر را پوشش داده اند."
    return base


FARSI_SOURCE_NAMES = {
    "ARN News Centre UAE": "ای آر ان نیوز",
    "Barq UAE Arabic": "برق امارات",
    "Dubai One / Emirates 24|7 UAE": "دبی وان / امارات ۲۴/۷",
    "Gulf News UAE": "گلف نیوز",
    "Khaleej Times UAE": "خلیج تایمز",
    "Lovin Dubai": "لاوین دبی",
    "The National UAE": "نشنال",
    "What's On Dubai": "واتس آن دبی",
}


def farsi_source_name(source: str) -> str:
    return FARSI_SOURCE_NAMES.get(source, source)


def farsi_sources_line(cluster: StoryCluster, limit: int | None = None) -> str:
    sources = cluster.sources[:limit] if limit else cluster.sources
    return "، ".join(farsi_source_name(source) for source in sources)


def farsi_digits(value: str) -> str:
    return value.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def farsi_money_detail(text: str) -> str:
    match = re.search(r"(?:aed|dh|dirham|درهم)\s*([0-9][0-9,]*)|([0-9][0-9,]*)\s*(?:aed|dh|dirham|درهم)", text, re.I)
    if not match:
        if re.search(r"100\s*ألف|100\s*الف|۱۰۰\s*هزار", text, re.I):
            return "۱۰۰ هزار درهم"
        return "مبلغ قابل توجهی"
    amount = (match.group(1) or match.group(2) or "").replace(",", "")
    suffix = ""
    if re.search(rf"{re.escape(match.group(0))}\s*(?:million|mn|مليون|ملیون)", text, re.I):
        suffix = " میلیون"
    elif re.search(rf"{re.escape(match.group(0))}\s*(?:billion|bn|مليار|میلیارد)", text, re.I):
        suffix = " میلیارد"
    return f"{farsi_digits(amount)}{suffix} درهم" if amount else "مبلغ قابل توجهی"


def farsi_count_detail(text: str) -> str:
    match = re.search(r"\b([0-9]{1,3})\b\s+(?:people|men|suspects|members|individuals)|arrest(?:ed|s)?\s+\b([0-9]{1,3})\b", text, re.I)
    if not match:
        return "چند نفر"
    count = match.group(1) or match.group(2)
    return f"{farsi_digits(count)} نفر"


def farsi_sentence(parts: list[str]) -> str:
    cleaned = [clean_text(part, 900).strip(" .،") for part in parts if clean_text(part, 900).strip(" .،")]
    if not cleaned:
        return ""
    return ". ".join(cleaned) + "."


def farsi_title_and_summary(cluster: StoryCluster) -> tuple[str, str, str]:
    raw_text = f"{cluster.title} {cluster.best_story.summary}"
    text = raw_text.lower()
    tags = set(cluster.tags)
    coverage = "چند رسانه معتبر همزمان این موضوع را پوشش داده اند، پس ارزش توجه بیشتری دارد. " if len(cluster.sources) > 1 else ""
    if re.search(r"fake|fraud|scam|booking scam|fake booking|کلاهبرداری|احتيال|مزيف", text, re.I):
        if re.search(r"travel|holiday|booking|flight|hotel|summer|سفر|هتل|پرواز", text, re.I):
            title = "هشدار پلیس دبی درباره رزروهای جعلی سفر"
            summary = "پلیس دبی درباره کلاهبرداری های سفر، بلیت و هتل هشدار داده است؛ مخصوصا پیشنهادهای تابستانی یا تخفیف هایی که در سایت ها و شبکه های اجتماعی جعلی منتشر می شوند. کلاهبرداران با پیشنهادهای فوری و قیمت های وسوسه کننده مردم را به پرداخت برای پرواز یا اقامت ناموجود ترغیب می کنند. نکته مهم این است که مخاطب قبل از پرداخت باید آدرس سایت، صفحه شبکه اجتماعی و واقعی بودن شرکت را بررسی کند."
            caption = "قبل از خرید سفر ارزان، واقعی بودن سایت و شرکت را چک کنید."
        else:
            title = "هشدار تازه درباره کلاهبرداری در امارات"
            summary = "مقام های رسمی درباره یک روش تازه کلاهبرداری یا پیشنهاد جعلی هشدار داده اند که می تواند برای ساکنان دبی دردسرساز شود. پیام اصلی این است که مردم قبل از پرداخت پول، ارسال اطلاعات شخصی یا کلیک روی لینک ها باید چند نشانه اعتماد را بررسی کنند. این خبر برای مخاطب کاربردی است چون مستقیما به امنیت مالی و رفتار روزمره در فضای آنلاین مربوط می شود."
            caption = "اگر پیشنهادی بیش از حد خوب به نظر می رسد، اول آن را بررسی کنید."
    elif re.search(r"ebola|ابولا", text, re.I):
        if re.search(r"travel warning|travel advisory|uganda|congo|south sudan|اوگاندا|کنگو|سودان جنوبی|twajudi", text, re.I):
            title = "هشدار سفر امارات درباره شیوع ابولا"
            summary = "امارات به شهروندان خود درباره سفر غیرضروری به اوگاندا، جمهوری دموکراتیک کنگو و سودان جنوبی به دلیل تحولات مربوط به ابولا هشدار داده است. در این اطلاعیه از مسافران خواسته شده در صورت نیاز به سفر، احتیاط کنند و از خدمات رسمی مانند تواجدي برای ثبت اطلاعات سفر استفاده کنند. اهمیت خبر برای مخاطب این است که هم جنبه سلامت عمومی دارد و هم می تواند روی برنامه سفر، امنیت مسافران و تصمیم خانواده ها اثر بگذارد."
            caption = "امارات درباره سفر به چند کشور آفریقایی به دلیل ابولا هشدار داد."
        else:
            title = "بررسی وضعیت ابولا و آمادگی سلامت در امارات"
            summary = "مقام های امارات تحولات مربوط به ابولا را بررسی کرده اند و تاکید دارند که وضعیت سلامت عمومی در کشور پایدار است. این خبر نشان می دهد نهادهای بهداشتی همچنان وضعیت را زیر نظر دارند و اقدامات آمادگی و پایش ادامه دارد. برای مخاطب، نکته اصلی آرامش همراه با آگاهی است؛ یعنی خبر جنبه هشدار دارد، اما پیام رسمی این است که وضعیت داخلی کنترل و رصد می شود."
            caption = "امارات می گوید وضعیت سلامت عمومی پایدار است و تحولات ابولا را رصد می کند."
    elif re.search(r"(return|returned|found|honesty|أمانت|عثر|سلم|سلّم)", text, re.I) and re.search(
        r"\baed\b|\bdh\b|dirham|درهم|100,?000|100 ألف|cash|money", text, re.I
    ):
        amount = farsi_money_detail(raw_text)
        if re.search(r"8,?700|8700|250 children", text, re.I):
            title = "هزاران نفر پول پیدا شده را به پلیس امارات تحویل دادند"
            summary = "بر اساس این خبر، بیش از ۸۷۰۰ نفر در سال ۲۰۲۵ پول پیدا شده را در سراسر امارات به پلیس تحویل داده اند و حتی بیش از ۲۵۰ کودک هم در میان این افراد بوده اند. محور اصلی خبر امانت داری، مسئولیت پذیری اجتماعی و اعتماد عمومی است. برای صفحه خبری، این موضوع یک زاویه مثبت و قابل اشتراک دارد چون تصویر خوبی از فرهنگ شهروندی و رفتار درست در امارات نشان می دهد."
            caption = "بیش از ۸۷۰۰ نفر در امارات پول پیدا شده را به پلیس تحویل دادند."
        else:
            title = "رفتار تحسین برانگیز در دبی پس از پیدا شدن پول"
            summary = f"این یک خبر مثبت محلی از دبی است؛ فردی {amount} یا یک مال گمشده را پیدا کرده و آن را به پلیس یا صاحبش برگردانده است. ماجرا روی امانت داری، اعتماد اجتماعی و واکنش مثبت پلیس یا جامعه تمرکز دارد. ارزش محتوایی آن در این است که تصویر انسانی و قابل اشتراک گذاری از زندگی روزمره در دبی می سازد."
            caption = "یک یادآوری خوب از امانت داری و اعتماد در دبی."
    elif re.search(r"solidarity|condolence|condemns|foreign|minister|تعزي|تتضامن|يدين", text, re.I):
        if re.search(r"kenya|fire|school|dormitory|کنیا|حریق|آتش", text, re.I):
            title = "همدردی امارات با کنیا پس از حادثه آتش سوزی"
            summary = "امارات پس از آتش سوزی مرگبار در یک خوابگاه دختران در کنیا، پیام همبستگی و تسلیت منتشر کرده است. این خبر بیشتر جنبه انسانی و دیپلماتیک دارد و نشان می دهد امارات به صورت رسمی با قربانیان، خانواده ها و دولت کنیا ابراز همدردی کرده است. برای صفحه خبری، زاویه اصلی می تواند همدلی، احترام و واکنش رسمی امارات باشد."
            caption = "امارات در پی حادثه تلخ کنیا پیام همدردی منتشر کرد."
        else:
            title = "موضع رسمی امارات درباره یک رویداد بین المللی"
            summary = "امارات در واکنش به یک اتفاق مهم بین المللی پیام همبستگی، تسلیت، محکومیت یا موضع رسمی منتشر کرده است. اهمیت خبر در نقش دیپلماسی امارات و پیام انسانی یا سیاسی این واکنش است. برای مخاطب فارسی زبان، بهتر است خبر با تاکید بر اینکه امارات چه گفته و چرا این واکنش مهم است روایت شود."
            caption = "واکنش رسمی امارات به یک خبر مهم بین المللی."
    elif "crime" in tags:
        if re.search(r"oud|عود", text, re.I) and re.search(r"theft|stole|steal|stealing|سرق|سرقت", text, re.I):
            amount = farsi_money_detail(raw_text)
            count = farsi_count_detail(raw_text)
            title = "بازداشت متهمان سرقت عود گران قیمت در دبی"
            summary = f"پلیس دبی تلاش یک باند برای سرقت عود گران قیمت را خنثی کرده و {count} را در ارتباط با این پرونده بازداشت کرده است. ارزش عود مورد هدف حدود {amount} گزارش شده است. بر اساس خبر، پرونده با عملیات پلیس برای شناسایی افراد درگیر و جلوگیری از سرقت این کالای گران قیمت پیگیری شده است."
            caption = "پلیس دبی پرونده سرقت عود گران قیمت را با بازداشت متهمان پیگیری کرد."
        elif re.search(r"arrest|arrested|قبض|ضبط", text, re.I):
            count = farsi_count_detail(raw_text)
            title = "بازداشت متهمان در یک پرونده پلیسی در دبی"
            summary = f"در این خبر، پلیس یا مقام های قضایی از بازداشت {count} در ارتباط با یک پرونده امنیتی یا جنایی خبر داده اند. اصل ماجرا درباره شناسایی متهمان، توضیح روش وقوع جرم و اقدام پلیس برای کنترل پرونده است. برای انتشار، خبر باید با خود اتفاق، محل و اقدام رسمی شروع شود، نه با تحلیل کلی درباره اهمیت امنیت."
            caption = "پلیس دبی از بازداشت متهمان یک پرونده تازه خبر داد."
        elif re.search(r"court|محكمة|دادگاه", text, re.I):
            title = "پرونده تازه در دادگاه های امارات"
            summary = "این خبر درباره یک پرونده قضایی در امارات است که در آن جزئیات اتهام، حکم یا روند رسیدگی دادگاه مطرح شده است. نکته اصلی برای مخاطب این است که بداند پرونده درباره چه اتفاقی بوده، مقام قضایی چه تصمیمی گرفته و نتیجه آن برای متهمان یا شاکیان چه بوده است."
            caption = "یک پرونده قضایی تازه در امارات خبرساز شد."
        else:
            title = "خبر تازه پلیسی یا امنیتی در امارات"
            summary = "این خبر درباره یک اتفاق پلیسی، امنیتی یا قضایی در امارات است. بر اساس جزئیات منتشرشده، یک نهاد رسمی وارد پرونده شده و موضوع به حادثه، متهمان، هشدار امنیتی یا روند قضایی مربوط می شود. در روایت خبر، تمرکز اصلی روی خود اتفاق و اقدام رسمی اعلام شده است."
            caption = "یک پرونده پلیسی تازه در امارات اعلام شد."
    elif "rules" in tags:
        if re.search(r"eswatini|إسواتيني|اسواتيني|visa waiver|mutual visa|الإعفاء المتبادل|تأشيرة الدخول", text, re.I):
            title = "توافق امارات و اسواتینی برای معافیت ویزا"
            summary = "امارات و پادشاهی اسواتینی تفاهم نامه ای برای معافیت متقابل از شرط ویزای ورود امضا کرده اند. چنین توافق هایی می توانند رفت وآمد، سفرهای کاری و روابط رسمی بین دو کشور را ساده تر کنند. برای مخاطب، نکته اصلی این است که بداند کدام کشورها درگیرند، موضوع ویزا چیست و چرا این تغییر برای سفر یا روابط بین المللی اهمیت دارد."
            caption = "امارات و اسواتینی برای ساده تر شدن رفت وآمد توافق ویزایی امضا کردند."
        else:
            title = "تغییر یا یادآوری مهم در قوانین و خدمات شهری"
            summary = "این خبر به یک تغییر، تصمیم یا یادآوری رسمی درباره قوانین، ویزا، جریمه ها، مجوزها، پارکینگ، سالک یا خدمات شهری امارات مربوط است. در متن پست باید دقیقا گفته شود چه قانون یا خدمتی مطرح شده، چه کسی آن را اعلام کرده و این تغییر از چه زمانی یا برای چه گروهی اهمیت دارد."
            caption = "یک تغییر رسمی و کاربردی در خدمات یا قوانین امارات اعلام شد."
    elif "weather/traffic" in tags:
        if re.search(r"41|۴۱|temperature|temperatures|fair skies|ncm|coastal", text, re.I):
            title = "پیش بینی هوای امارات با کاهش دما در مناطق ساحلی"
            summary = "پیش بینی هواشناسی امارات از آسمان نسبتا صاف و دمای بالا خبر می دهد؛ در ابوظبی دما می تواند به حدود ۴۱ درجه برسد. مرکز ملی هواشناسی همچنین اشاره کرده که روز یکشنبه کاهش دما، به خصوص در مناطق ساحلی، انتظار می رود. اهمیت خبر برای مخاطب در برنامه ریزی روزانه، زمان بیرون رفتن، سفرهای کوتاه و آمادگی برای گرماست."
            caption = "هوای امارات همچنان گرم است، اما در مناطق ساحلی کاهش دما پیش بینی شده."
        else:
            title = "اطلاع رسانی کاربردی درباره آب وهوا یا رفت وآمد"
            summary = "این خبر یک به روزرسانی درباره آب وهوا، جاده ها، ترافیک، پروازها یا رفت وآمد در دبی و امارات است. خلاصه پست باید زمان، مکان و تغییر اصلی را روشن بگوید؛ مثلا دما، بارش، مسیر، تاخیر یا توصیه رسمی که در خبر آمده است."
            caption = "یک به روزرسانی تازه درباره آب وهوا یا رفت وآمد در امارات."
    elif "lifestyle" in tags:
        if re.search(r"cafe|caf[eé]|alserkal", text, re.I):
            title = "کافه های دیدنی دبی برای لیست آخر هفته"
            summary = "این خبر چند کافه یا تجربه غذایی در دبی را معرفی می کند که برای محتوای سبک زندگی و پیشنهادهای آخر هفته مناسب است. زاویه اصلی برای مخاطب این است که بداند کجا می تواند یک قرار، قهوه، فضای عکاسی یا تجربه شهری تازه داشته باشد. برای پست اینستاگرام، بهتر است روی حس مکان، فضای متفاوت و دلیل رفتن به آن کافه ها تمرکز شود."
            caption = "چند کافه دبی که ارزش اضافه شدن به لیست آخر هفته را دارند."
        elif re.search(r"steakhouse|steak|wagyu|tomahawk", text, re.I):
            title = "بهترین استیک هاوس های دبی برای عاشقان غذا"
            summary = "این خبر درباره رستوران ها و استیک هاوس های دبی است؛ از انتخاب های لوکس و واگیو گرفته تا تجربه های مناسب شام و مناسبت های خاص. برای مخاطب مجله ای، ارزش خبر در این است که یک راهنمای سریع برای انتخاب رستوران می سازد و می تواند به شکل پست ذخیره کردنی منتشر شود. زاویه خوب برای کپشن، سوال درباره بهترین استیک شهر یا معرفی چند گزینه برای آخر هفته است."
            caption = "اگر دنبال یک شام خاص در دبی هستید، این لیست به دردتان می خورد."
        elif re.search(r"brunch", text, re.I):
            title = "برانچ های معروف دبی که هنوز سر زبان ها هستند"
            summary = "این خبر روی برانچ ها و تجربه های غذایی آخر هفته در دبی تمرکز دارد؛ موضوعی که برای مخاطب محلی، گردشگران و علاقه مندان سبک زندگی جذاب است. ارزش محتوایی آن در قابل ذخیره بودن است، چون مخاطب می تواند از آن برای برنامه آخر هفته یا انتخاب رستوران استفاده کند. بهتر است پست با حس تجربه، قیمت یا حال وهوای مکان روایت شود."
            caption = "یک ایده خوشمزه برای برنامه آخر هفته در دبی."
        elif re.search(r"wine|bar|nightlife|entertainment", text, re.I):
            title = "یک گزینه تازه برای شب گردی و تجربه غذایی در دبی"
            summary = "این خبر یک بار، رستوران یا فضای شبانه در دبی را معرفی می کند و برای محتوای سبک زندگی شهری مناسب است. اهمیت آن برای صفحه مجله ای این است که فقط خبر افتتاح یا معرفی مکان نیست؛ می تواند به مخاطب ایده بدهد کجا برود، چه فضایی انتظار داشته باشد و چرا این تجربه متفاوت است. در کپشن بهتر است روی حال وهوای مکان و مناسب بودن برای قرار یا دورهمی تمرکز شود."
            caption = "یک آدرس تازه برای شب های دبی و قرارهای خاص."
        elif re.search(r"pool|beach|resort", text, re.I):
            title = "استخرها و تجربه های آفتابی دبی برای آخر هفته"
            summary = "این خبر درباره استخرها، ریزورت ها یا تجربه های فضای باز در دبی است و برای پست های تصویری بسیار مناسب است. ارزش آن در این است که مخاطب می تواند از آن برای برنامه ریزی آخر هفته، انتخاب لوکیشن عکاسی یا یک روز آرام در شهر استفاده کند. زاویه خوب برای انتشار، ترکیب تصویر قوی، حس تابستانی و یک سوال کوتاه از مخاطب است."
            caption = "یک ایده تصویری و جذاب برای آخر هفته در دبی."
        else:
            title = "پیشنهاد تازه برای سبک زندگی در دبی"
            summary = "این خبر یک پیشنهاد مشخص از سبک زندگی دبی را معرفی می کند؛ مثل رستوران، کافه، رویداد، مرکز خرید، پاپ آپ یا برنامه آخر هفته. خلاصه پست باید بگوید این پیشنهاد چیست، در کجای دبی قرار دارد یا چه تجربه ای به مخاطب می دهد."
            caption = "یک پیشنهاد تازه برای تجربه کردن دبی."
    elif "viral" in tags:
        if re.search(r"paragliding|ice-cream|ice cream|landlord", text, re.I):
            title = "چند سوژه وایرال از دبی در یک خبر"
            summary = "این خبر چند موضوع وایرال و سبک تر از دبی را کنار هم آورده است؛ از حادثه پاراگلایدینگ گرفته تا بستنی رایگان و رفتار مثبت یک صاحبخانه. ارزش این نوع خبر در این است که برای شبکه های اجتماعی سریع، قابل اشتراک و مناسب شروع گفت وگو است. برای صفحه مجله، می توان آن را به شکل یک راندآپ کوتاه از چیزهایی که امروز در دبی سر زبان هاست منتشر کرد."
            caption = "از حادثه وایرال تا بستنی رایگان؛ این ها امروز در دبی خبرساز شدند."
        else:
            title = "موضوعی که در دبی پتانسیل وایرال شدن دارد"
            summary = "این خبر درباره یک سوژه اجتماعی، تصویری یا پربحث در دبی است که در شبکه های اجتماعی می تواند توجه بگیرد. خلاصه پست باید خود اتفاق را روشن بگوید: چه چیزی دیده شده، چه کسی یا کجا درگیر است و چرا مردم درباره آن حرف می زنند."
            caption = "یک سوژه تازه از دبی که می تواند در شبکه های اجتماعی دیده شود."
    elif "business" in tags:
        if re.search(r"gdp|economy|اقتصاد|الناتج المحلي|6\\.2|6,2|1\\.9|1,9|trillion|تريليون", text, re.I):
            title = "رشد تازه اقتصاد امارات"
            summary = "این خبر می گوید اقتصاد امارات رشد تازه ای ثبت کرده و تولید ناخالص داخلی کشور به سطح بالاتری رسیده است. بخش هایی مثل ساخت وساز، مالی، املاک، گردشگری یا سرمایه گذاری می توانند در این تصویر اقتصادی نقش داشته باشند. نکته مهم برای مخاطب این است که چنین خبرهایی فقط عدد اقتصادی نیستند؛ می توانند روی فرصت های شغلی، بازار ملک، فضای کسب وکار و اعتماد سرمایه گذاران اثر بگذارند."
            caption = "اقتصاد امارات دوباره خبرساز شد؛ عددها چه پیامی دارند؟"
        else:
            title = "خبر مهم اقتصادی یا کسب وکاری در دبی"
            summary = "این خبر به اقتصاد دبی، بازار ملک، سرمایه گذاری، استارتاپ ها، فرصت های شغلی یا فضای کسب وکار مربوط است. خلاصه پست باید عدد، تصمیم، شرکت، پروژه یا بخش اقتصادی مطرح شده در خبر را واضح توضیح دهد و بعد نتیجه احتمالی آن را کوتاه بیان کند."
            caption = "یک خبر تازه از اقتصاد و کسب وکار دبی."
    else:
        title = "به روزرسانی تازه از دبی و امارات"
        summary = "این یک خبر تازه درباره دبی یا امارات است. خلاصه باید روی خود اتفاق تمرکز کند: چه چیزی اعلام شده، کجا رخ داده، چه کسی درگیر است و نتیجه اولیه خبر چیست."
        caption = "یک خبر تازه از امارات که ارزش دنبال کردن دارد."
    return title, farsi_sentence([coverage, summary]), caption


def farsi_brief(cluster: StoryCluster) -> str:
    _, full_summary, _ = farsi_title_and_summary(cluster)
    return f"خلاصه کامل: {full_summary}"


def fallback_editorial_package(cluster: StoryCluster) -> dict[str, str]:
    summary = caption_summary(cluster)
    idea = post_suggestion(cluster)
    return {
        "headline": clean_text(cluster.title, 120),
        "caption": summary,
        "farsi": farsi_brief(cluster),
        "post_idea": idea,
        "image_suggestion": image_suggestion(cluster),
        "image_prompt": image_prompt(cluster),
        "persian_social": persian_social_pack(cluster),
        "copy_ready": copy_ready_post_block(cluster),
        "priority": priority_label(cluster),
        "carousel_title": clean_text(cluster.title, 70),
        "why_care": why_care(cluster),
        "calendar_slot": calendar_slot(cluster),
    }


def ai_editorial_package(conn: sqlite3.Connection | None, cluster: StoryCluster) -> dict[str, str]:
    return fallback_editorial_package(cluster)


def post_suggestion(cluster: StoryCluster) -> str:
    text = f"{cluster.title} {cluster.best_story.summary}".lower()
    tags = set(cluster.tags)
    if re.search(r"solidarity|condolence|condemns|foreign|minister|تعزي|تتضامن|يدين", text, re.I):
        return "زاویه پست: خبر را به عنوان واکنش رسمی امارات با تمرکز بر جنبه انسانی منتشر کنید."
    if re.search(r"(return|returned|found|honesty|أمانت|عثر|سلم|سلّم)", text, re.I) and re.search(
        r"\baed\b|\bdh\b|dirham|درهم|100,?000|100 ألف", text, re.I
    ):
        return "زاویه پست: آن را به شکل یک خبر مثبت درباره امانت داری در دبی روایت کنید."
    if re.search(r"fake|fraud|scam|warn", text, re.I):
        return "زاویه پست: آن را به شکل هشدار کاربردی با نکات احتیاطی منتشر کنید."
    if "breaking" in tags:
        return "زاویه پست: با خود اتفاق، محل وقوع و اقدام لازم برای ساکنان شروع کنید."
    if "viral" in tags:
        return "زاویه پست: توضیح دهید چرا این سوژه امروز در دبی بحث برانگیز شده است."
    if "lifestyle" in tags:
        return "زاویه پست: آن را به عنوان پیشنهاد آخر هفته یا تجربه تازه در دبی معرفی کنید."
    if "rules" in tags:
        return "زاویه پست: تغییر عملی و کسانی را که تحت تاثیر قرار می گیرند ساده توضیح دهید."
    if "weather/traffic" in tags:
        return "زاویه پست: زمان، مکان و توصیه اصلی را کوتاه و کاربردی بگویید."
    if "business" in tags:
        return "زاویه پست: آن را به عنوان روند اقتصادی یا کسب وکاری مهم در دبی معرفی کنید."
    return "زاویه پست: خبر را با یک نکته روشن و کوتاه برای مخاطب دبی روایت کنید."


def image_suggestion(cluster: StoryCluster) -> str:
    text = f"{cluster.title} {cluster.best_story.summary}".lower()
    tags = set(cluster.tags)
    if re.search(r"fake|fraud|scam|booking scam|fake booking|کلاهبرداری|احتيال|مزيف", text, re.I):
        return "ایده تصویر اچ دی: نمای نزدیک از موبایل با صفحه رزرو سفر عمومی، کنار پاسپورت و چمدان، نور آپارتمان در دبی، بدون لوگو و بدون اسکرین شات واقعی."
    if re.search(r"ebola|ابولا|health|public health", text, re.I):
        return "ایده تصویر اچ دی: فضای تمیز فرودگاه یا کلینیک در امارات با مسافر ماسک دار، پاسپورت و صفحه هشدار سلامت عمومی، بدون بیمار و بدون لوگو."
    if re.search(r"(return|returned|found|honesty|cash|money|أمانت|عثر|سلم|سلّم)", text, re.I):
        return "ایده تصویر اچ دی: نمای محترمانه از دستی که کیف پول یا پاکت را روی کانتر خدمات پلیس تحویل می دهد، فضای مدنی دبی، بدون چهره و بدون لوگو."
    if re.search(r"solidarity|condolence|condemns|foreign|minister|تعزي|تتضامن|يدين", text, re.I):
        return "ایده تصویر اچ دی: پرچم امارات کنار میز دیپلماتیک ساده با گل یا دفتر تسلیت، نور نرم و محترمانه، بدون تصویر قربانیان و بدون مهر رسمی."
    if "weather/traffic" in tags:
        return "ایده تصویر اچ دی: خط آسمان دبی در روز آفتابی با حس گرما، جاده یا ساحل در پیش زمینه، سبک خبری تمیز، بدون گرافیک خبری و برند رسانه."
    if "rules" in tags:
        return "ایده تصویر اچ دی: چیدمان مینیمال اداری با پاسپورت، مفهوم مهر ورود امارات، کارت مترو یا پارکینگ، بدون سند واقعی و بدون اطلاعات شخصی."
    if "business" in tags:
        return "ایده تصویر اچ دی: خط آسمان منطقه تجاری دبی با بازتاب نمودارهای مالی روی شیشه، سبک مجله ای حرفه ای، بدون لوگوی شرکت."
    if "lifestyle" in tags:
        return "ایده تصویر اچ دی: صحنه سبک زندگی دبی با میز کافه، نور شهر، کیسه خرید یا دستبند رویداد، ظاهر گرم و شیک، بدون برند قابل تشخیص."
    if "viral" in tags:
        return "ایده تصویر اچ دی: صحنه پویا از خیابان دبی با موبایلی که یک لحظه عمومی شهری را ضبط می کند، بدون اسکرین شات پست و بدون لوگوی پلتفرم."
    if "crime" in tags:
        return "ایده تصویر اچ دی: تصویر خنثی امنیت عمومی با خیابان محو دبی، نور هشدار ملایم و اعلان موبایل، بدون صحنه جرم و بدون افراد قابل شناسایی."
    return "ایده تصویر اچ دی: تصویر تمیز و مجله ای از دبی مرتبط با موضوع خبر، مدرن و با کیفیت بالا، کاملا اورجینال، بدون لوگو و بدون کپی از عکس خبر."


def image_prompt(cluster: StoryCluster) -> str:
    text = f"{cluster.title} {cluster.best_story.summary}".lower()
    tags = set(cluster.tags)
    if re.search(r"fake|fraud|scam|booking scam|fake booking|کلاهبرداری|احتيال|مزيف", text, re.I):
        scene = "نمای نزدیک از موبایلی با صفحه رزرو سفر به شکل عمومی، کنار پاسپورت و چمدان، با نور طبیعی آپارتمان در دبی"
    elif re.search(r"ebola|ابولا|health|public health", text, re.I):
        scene = "فضای تمیز فرودگاه یا کلینیک در امارات، یک مسافر با ماسک، پاسپورت و مانیتور هشدار سلامت به شکل عمومی"
    elif re.search(r"(return|returned|found|honesty|cash|money|أمانت|عثر|سلم|سلّم)", text, re.I):
        scene = "نمای محترمانه از دستی که کیف پول یا پاکت پول را روی کانتر خدمات پلیس تحویل می دهد، بدون نمایش چهره"
    elif re.search(r"solidarity|condolence|condemns|foreign|minister|تعزي|تتضامن|يدين", text, re.I):
        scene = "پرچم امارات کنار میز دیپلماتیک ساده با گل یا دفتر تسلیت، نور نرم و محترمانه"
    elif "weather/traffic" in tags:
        scene = "خط آسمان دبی در یک روز آفتابی با حس گرما، جاده یا ساحل در پیش زمینه، سبک عکس خبری تمیز"
    elif "rules" in tags:
        scene = "چیدمان مینیمال از پاسپورت، کارت حمل ونقل یا پارکینگ و میز اداری تمیز، بدون اطلاعات واقعی شخصی"
    elif "business" in tags:
        scene = "منطقه تجاری مدرن دبی با بازتاب نمودارهای مالی روی شیشه، سبک مجله ای حرفه ای"
    elif "crime" in tags:
        scene = "تصویر خنثی امنیت عمومی با خیابان دبی در پس زمینه، نورهای ملایم هشدار و اعلان موبایل، بدون صحنه جرم"
    elif "lifestyle" in tags:
        scene = "صحنه سبک زندگی دبی با میز کافه، نور شهر، حس رستوران یا رویداد آخر هفته، ظاهر شیک و گرم"
    elif "viral" in tags:
        scene = "صحنه پویا از خیابان دبی با موبایلی که یک لحظه شهری عمومی را ضبط می کند، پرانرژی و مناسب شبکه اجتماعی"
    else:
        scene = "تصویر تمیز و مجله ای از دبی که با موضوع خبر ارتباط دارد و حس مدرن و خبری داشته باشد"
    return (
        f"{scene}. تصویر با کیفیت بالا و اچ دی برای پست شبکه اجتماعی بساز؛ نسبت عمودی ۴:۵، سبک مجله آنلاین دبی، "
        "واقع گرایانه اما کاملا اورجینال، جزئیات شارپ، نور طبیعی، بدون متن روی تصویر، بدون لوگو، بدون واترمارک، "
        "بدون کپی از عکس خبر و بدون چهره قابل شناسایی افراد عادی."
    )


def persian_social_pack(cluster: StoryCluster) -> str:
    _, _, _ = farsi_title_and_summary(cluster)
    tags = set(cluster.tags)
    if "crime" in tags:
        hook = "جزئیات این پرونده پلیسی در دبی را کوتاه و روشن ببینید."
    elif "viral" in tags:
        hook = "به نظرتون این سوژه در دبی چرا اینقدر سریع وایرال می شود؟"
    elif "weather/traffic" in tags:
        hook = "اگر امروز در امارات برنامه بیرون رفتن دارید، این خبر را از دست ندهید."
    elif "business" in tags:
        hook = "این عددها فقط خبر اقتصادی نیستند؛ می توانند روی بازار و زندگی روزمره اثر بگذارند."
    elif "rules" in tags:
        hook = "این تغییر ممکن است برای سفر، رانندگی یا کارهای روزمره شما مهم باشد."
    else:
        hook = "یک خبر تازه از امارات که ارزش دنبال کردن دارد."
    hashtags = ["#دبی", "#امارات", "#اخبار_دبی"]
    if "crime" in tags:
        hashtags.append("#امنیت_دبی")
    elif "viral" in tags:
        hashtags.append("#دبی_وایرال")
    elif "business" in tags:
        hashtags.append("#اقتصاد_امارات")
    elif "weather/traffic" in tags:
        hashtags.append("#آب_وهوای_امارات")
    elif "lifestyle" in tags:
        hashtags.append("#زندگی_در_دبی")
    return "\n".join(
        [
            f"هوک ریل: {hook}",
            f"هشتگ ها: {' '.join(hashtags[:4])}",
        ]
    )


def copy_ready_post_block(cluster: StoryCluster) -> str:
    _, _, caption = farsi_title_and_summary(cluster)
    social = persian_social_pack(cluster)
    hashtags = "#دبی #امارات #اخبار_دبی"
    for line in social.splitlines():
        if line.startswith("هشتگ ها:"):
            hashtags = line.split(":", 1)[1].strip()
            break
    return "\n".join(
        [
            f"کپشن: {caption}",
            f"هشتگ: {hashtags}",
            f"پرامپت تصویر: {clean_text(image_prompt(cluster), 520)}",
            f"لینک: {cluster.best_story.link}",
        ]
    )


def priority_label(cluster: StoryCluster) -> str:
    tags = set(cluster.tags)
    if cluster.score >= 18 or len(cluster.sources) >= 3:
        return "اولویت بالا"
    if "viral" in tags and cluster.score >= 14:
        return "پتانسیل وایرال"
    if "breaking" in tags or "weather/traffic" in tags or "crime" in tags:
        return "مناسب امروز"
    if "business" in tags or "rules" in tags:
        return "مناسب توضیح کوتاه"
    return "مناسب راندآپ"


def why_care(cluster: StoryCluster) -> str:
    tags = set(cluster.tags)
    if "crime" in tags:
        return "برای ساکنان مهم است چون به امنیت، کلاهبرداری یا ریسک قانونی مربوط می شود."
    if "viral" in tags:
        return "برای تعامل خوب است چون ظرفیت بحث و واکنش در شبکه های اجتماعی دارد."
    if "lifestyle" in tags:
        return "برای برنامه آخر هفته و محتوای سریع سبک زندگی مناسب است."
    if "rules" in tags:
        return "کاربردی است چون می تواند روی رفت وآمد، سفر، پرداخت یا رعایت قانون اثر بگذارد."
    if "weather/traffic" in tags:
        return "به موقع است چون به برنامه ریزی روزانه ساکنان کمک می کند."
    if "business" in tags:
        return "برای دنبال کردن روند اقتصاد، کسب وکار یا استارتاپ های دبی مفید است."
    return "یک به روزرسانی به موقع از دبی با ارتباط روشن برای مخاطب."


def calendar_slot(cluster: StoryCluster) -> str:
    tags = set(cluster.tags)
    if "breaking" in tags or "weather/traffic" in tags:
        return "امروز به عنوان به روزرسانی کوتاه منتشر شود."
    if "viral" in tags:
        return "امروز به شکل ریل یا کپشن کوتاه منتشر شود."
    if "lifestyle" in tags:
        return "برای راندآپ آخر هفته یا کاروسل نگه داشته شود."
    if "business" in tags or "rules" in tags:
        return "فردا به شکل کاروسل توضیحی استفاده شود."
    return "برای راندآپ روزانه نگه داشته شود."


def group_key(title: str) -> str:
    tokens = sorted(normalized_tokens(title))
    raw = " ".join(tokens[:14]) or title.lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_clusters(stories: list[Story], min_similarity: float = 0.55) -> list[StoryCluster]:
    clusters: list[StoryCluster] = []
    for story in stories:
        best_cluster = None
        best_score = 0.0
        for cluster in clusters:
            similarity = cluster_similarity(cluster, story)
            if similarity > best_score:
                best_cluster = cluster
                best_score = similarity
        if best_cluster and best_score >= min_similarity:
            best_cluster.stories.append(story)
            if story.score > best_cluster.best_story.score:
                best_cluster.title = story.title
        else:
            clusters.append(
                StoryCluster(
                    key=group_key(story.title),
                    title=story.title,
                    stories=[story],
                    score=story.score,
                    reasons=list(story.reasons),
                    tags=classify_story(story),
                )
            )

    for cluster in clusters:
        sources_bonus = min(6, max(0, len(cluster.sources) - 1) * 3)
        tags = sorted({tag for story in cluster.stories for tag in classify_story(story)})
        reasons = []
        for story in cluster.stories:
            for reason in story.reasons:
                if reason not in reasons:
                    reasons.append(reason)
        if sources_bonus:
            reasons.insert(0, f"{len(cluster.sources)} sources +{sources_bonus}")
        cluster.score = max(story.score for story in cluster.stories) + sources_bonus
        cluster.reasons = reasons[:7]
        cluster.tags = tags[:4]
        cluster.key = group_key(cluster.title)

    return sorted(clusters, key=lambda item: (item.score, item.best_story.published), reverse=True)


def clean_url(url: str, base_url: str) -> str:
    absolute = urljoin(base_url, html.unescape(url or ""))
    parsed = urlparse(absolute)
    query = urlencode(
        [(key, value) for key, value in parse_qsl(parsed.query) if not key.lower().startswith("utm_")]
    )
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", query, ""))


def url_allowed(url: str, source: dict[str, Any]) -> bool:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    include_paths = [item.rstrip("/") for item in source.get("include_paths", [])]
    if include_paths and not any(path.startswith(item) for item in include_paths):
        return False
    if path in include_paths:
        return False
    if source.get("require_url_date") and not date_from_url(url):
        return False
    if any(term in url for term in source.get("exclude_url_terms", [])):
        return False
    return True


def story_from_page_candidate(
    source: dict[str, Any],
    config: dict[str, Any],
    title: str,
    url: str,
    summary: str = "",
    published: Any = None,
    image_url: str = "",
) -> Story | None:
    title = clean_text(title, 180)
    summary = clean_text(summary)
    if not title or len(title) < int(source.get("min_title_length", 24)):
        return None
    if title.lower() in {"home", "uae", "dubai", "latest news", "read more", "أكمل القراءة"}:
        return None
    link = clean_url(url, source["url"])
    if not url_allowed(link, source):
        return None
    url_dt = date_from_url(link)
    published_known = bool(published or url_dt)
    published_dt = parse_dt(published) if published else url_dt or utcnow()
    entry = {
        "title": title,
        "summary": summary,
        "link": link,
        "published": published_dt,
        "_published_known": published_known,
    }
    score, reasons = score_story(entry, source, config)
    return Story(
        source=source.get("name", "Unknown"),
        title=title,
        link=link,
        summary=summary,
        published=published_dt,
        score=score,
        reasons=reasons,
        image_url=image_url,
    )


def walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def json_blocks(soup: BeautifulSoup) -> list[Any]:
    blocks: list[Any] = []
    for tag in soup.find_all("script"):
        raw = tag.string or tag.get("data-page")
        if not raw and tag.get("id") == "app":
            raw = tag.get("data-page")
        if not raw:
            continue
        raw = html.unescape(raw).strip()
        if not raw or raw[0] not in "[{":
            continue
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    app = soup.find(id="app")
    if app and app.get("data-page"):
        try:
            blocks.append(json.loads(html.unescape(app["data-page"])))
        except json.JSONDecodeError:
            pass
    return blocks


def collect_page(source: dict[str, Any], config: dict[str, Any]) -> list[Story]:
    resp = requests.get(source["url"], headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    stories_by_link: dict[str, Story] = {}

    for block in json_blocks(soup):
        for item in walk_json(block):
            title = item.get("headline") or item.get("title") or item.get("name")
            url = item.get("url") or item.get("canonical-url") or item.get("slug") or item.get("original_url")
            if not title or not url:
                continue
            summary = item.get("summary") or item.get("subheadline") or item.get("description") or ""
            image_url = find_image_url(item, source["url"])
            published = (
                item.get("published-at")
                or item.get("first-published-at")
                or item.get("publishedAt")
                or item.get("datePublished")
            )
            story = story_from_page_candidate(source, config, str(title), str(url), str(summary), published, image_url)
            if story:
                stories_by_link.setdefault(story.link, story)

    for anchor in soup.find_all("a", href=True):
        title = " ".join(anchor.get_text(" ", strip=True).split())
        story = story_from_page_candidate(source, config, title, anchor["href"])
        if story:
            stories_by_link.setdefault(story.link, story)

    return list(stories_by_link.values())


def score_story(entry: dict[str, Any], source: dict[str, Any], config: dict[str, Any]) -> tuple[int, list[str]]:
    title = clean_text(entry.get("title", ""), 500)
    summary = clean_text(entry.get("summary", "") or entry.get("description", ""), 500)
    text = f"{title} {summary}"
    score = int(source.get("weight", 0))
    reasons = []
    if source.get("weight"):
        reasons.append(f"trusted source +{source.get('weight')}")

    published = parse_dt(entry.get("published") or entry.get("updated"))
    if entry.get("_published_known", True):
        age_hours = max(0, (utcnow() - published).total_seconds() / 3600)
        if age_hours <= 1:
            score += 6
            reasons.append("fresh <1h")
        elif age_hours <= 6:
            score += 4
            reasons.append("fresh <6h")
        elif age_hours <= 24:
            score += 2
            reasons.append("fresh today")

    extra, keyword_reasons = keyword_score(text, config.get("keywords", []))
    score += extra
    reasons.extend(keyword_reasons)

    if re.search(r"\b(dubai|uae|emirates|دبي|الإمارات|الامارات)\b", text, re.I):
        score += 2
        reasons.append("Dubai/UAE")

    if re.search(r"\b(video|watch|live|breaking|exclusive|viral|trending|most read|most viewed|عاجل|فيديو)\b", text, re.I):
        score += 2
        reasons.append("attention signal")

    return score, reasons[:6]


def collect(config: dict[str, Any], hours: int) -> list[Story]:
    stories: list[Story] = []
    cutoff = utcnow().timestamp() - hours * 3600
    for source in config.get("sources", []):
        if source.get("enabled") is False:
            continue
        if source.get("type") == "page":
            for story in collect_page(source, config):
                if story.published.timestamp() >= cutoff:
                    stories.append(story)
            continue
        parsed = feedparser.parse(source["url"])
        for entry in parsed.entries:
            published = parse_dt(entry.get("published") or entry.get("updated"))
            if published.timestamp() < cutoff:
                continue
            title = clean_text(entry.get("title", ""), 180)
            link = entry.get("link", "")
            if not title or not link:
                continue
            summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
            geo_terms = [] if source.get("skip_geo_filter") else config.get("require_any_terms", [])
            combined = f"{title} {summary}".lower()
            if geo_terms and not any(term.lower() in combined for term in geo_terms):
                continue
            entry["_published_known"] = True
            score, reasons = score_story(entry, source, config)
            stories.append(
                Story(
                    source=source.get("name", parsed.feed.get("title", "Unknown")),
                    title=title,
                    link=link,
                    summary=summary,
                    published=published,
                    score=score,
                    reasons=reasons,
                    image_url=rss_image_url(entry),
                )
            )
    return sorted(stories, key=lambda item: (item.score, item.published), reverse=True)


def telegram_call(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(data)
    return data


def send_html_message(
    token: str,
    chat_id: str,
    text: str,
    disable_web_page_preview: bool = True,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    lines = text.splitlines()
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= TELEGRAM_TEXT_LIMIT:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = line
    if current:
        chunks.append(current)

    for idx, chunk in enumerate(chunks or [text]):
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_web_page_preview,
        }
        if idx == 0 and reply_markup:
            payload["reply_markup"] = reply_markup
        telegram_call(token, "sendMessage", payload)


def feedback_keyboard(cluster: StoryCluster) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "مفید", "callback_data": f"fb:{cluster.key}:useful"},
                {"text": "کم اهمیت", "callback_data": f"fb:{cluster.key}:boring"},
                {"text": "دیر", "callback_data": f"fb:{cluster.key}:late"},
                {"text": "بیشتر", "callback_data": f"fb:{cluster.key}:more"},
            ],
            [
                {"text": "تایید", "callback_data": f"act:{cluster.key}:approve"},
                {"text": "رد", "callback_data": f"act:{cluster.key}:skip"},
                {"text": "بازنویسی", "callback_data": f"act:{cluster.key}:rewrite"},
                {"text": "بعدا", "callback_data": f"act:{cluster.key}:later"},
            ]
        ]
    }


def format_cluster(cluster: StoryCluster, conn: sqlite3.Connection | None = None) -> str:
    editorial = ai_editorial_package(conn, cluster)
    title, _, _ = farsi_title_and_summary(cluster)
    source_line = farsi_sources_line(cluster)
    links = "\n".join(
        f"{idx + 1}. <a href=\"{html.escape(link)}\">{html.escape(urlparse(link).netloc)}</a>"
        for idx, link in enumerate(cluster.links[:4])
    )
    return (
        f"<b>{html.escape(title)}</b>\n"
        f"منبع: {html.escape(source_line)}\n\n"
        f"{html.escape(editorial['farsi'])}\n\n"
        f"<b>پک کپشن فارسی:</b>\n{html.escape(editorial['persian_social'])}\n\n"
        f"<b>آماده کپی برای پست:</b>\n{html.escape(editorial['copy_ready'])}\n\n"
        f"<b>لینک خبر:</b>\n"
        f"{links}"
    )


def format_digest(clusters: list[StoryCluster], conn: sqlite3.Connection | None = None) -> str:
    lines = [
        "<b>رادار مجله دبی</b>",
        f"{farsi_digits(str(len(clusters)))} خبر مهم پیدا شد",
        "",
    ]
    for idx, cluster in enumerate(clusters, 1):
        best = cluster.best_story
        editorial = ai_editorial_package(conn, cluster)
        title, _, _ = farsi_title_and_summary(cluster)
        source_line = farsi_sources_line(cluster, 3)
        lines.extend(
            [
                f"<b>{farsi_digits(str(idx))}. {html.escape(title)}</b>",
                f"منبع: {html.escape(source_line)}",
                html.escape(editorial["farsi"]),
                f"<b>پک کپشن فارسی:</b>\n{html.escape(editorial['persian_social'])}",
                f"<b>آماده کپی برای پست:</b>\n{html.escape(editorial['copy_ready'])}",
                f"<a href=\"{html.escape(best.link)}\">لینک خبر</a>",
                "",
            ]
        )
    return "\n".join(lines).strip()


def format_today(clusters: list[StoryCluster], conn: sqlite3.Connection | None = None, limit: int = 5) -> str:
    lines = [
        "<b>برنامه پست امروز</b>",
        "بهترین خبرها برای آماده کردن پست امروز.",
        "",
    ]
    for idx, cluster in enumerate(clusters[:limit], 1):
        best = cluster.best_story
        editorial = ai_editorial_package(conn, cluster)
        title, _, _ = farsi_title_and_summary(cluster)
        source_line = farsi_sources_line(cluster, 3)
        lines.extend(
            [
                f"<b>{farsi_digits(str(idx))}. {html.escape(title)}</b>",
                f"منبع: {html.escape(source_line)}",
                html.escape(editorial["farsi"]),
                f"<b>پک کپشن فارسی:</b>\n{html.escape(editorial['persian_social'])}",
                f"<b>آماده کپی برای پست:</b>\n{html.escape(editorial['copy_ready'])}",
                f"<a href=\"{html.escape(best.link)}\">لینک خبر</a>",
                "",
            ]
        )
    return "\n".join(lines).strip()


def send_cluster(token: str, chat_id: str, cluster: StoryCluster, conn: sqlite3.Connection | None = None) -> None:
    image_url = cluster_image_url(cluster)
    if image_url:
        try:
            telegram_call(
                token,
                "sendPhoto",
                {
                    "chat_id": chat_id,
                    "photo": image_url,
                    "caption": html.escape(clean_text(farsi_title_and_summary(cluster)[0], 900)),
                    "parse_mode": "HTML",
                },
            )
        except Exception:
            pass
    send_html_message(
        token,
        chat_id,
        format_cluster(cluster, conn),
        disable_web_page_preview=False,
        reply_markup=feedback_keyboard(cluster),
    )


def send_today(token: str, chat_id: str, clusters: list[StoryCluster], conn: sqlite3.Connection | None = None, limit: int = 5) -> None:
    telegram_call(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": "<b>برنامه پست امروز</b>\nبهترین خبرها برای آماده کردن پست امروز.",
            "parse_mode": "HTML",
        },
    )
    for idx, cluster in enumerate(clusters[:limit], 1):
        best = cluster.best_story
        editorial = ai_editorial_package(conn, cluster)
        title, _, _ = farsi_title_and_summary(cluster)
        source_line = farsi_sources_line(cluster, 3)
        text = "\n".join(
            [
                f"<b>{farsi_digits(str(idx))}. {html.escape(title)}</b>",
                f"منبع: {html.escape(source_line)}",
                "",
                html.escape(editorial["farsi"]),
                "",
                f"<b>پک کپشن فارسی:</b>\n{html.escape(editorial['persian_social'])}",
                "",
                f"<b>آماده کپی برای پست:</b>\n{html.escape(editorial['copy_ready'])}",
                "",
                f"<a href=\"{html.escape(best.link)}\">لینک خبر</a>",
            ]
        )
        send_html_message(token, chat_id, text, disable_web_page_preview=False)


def send_digest(token: str, chat_id: str, clusters: list[StoryCluster], conn: sqlite3.Connection | None = None) -> None:
    send_html_message(token, chat_id, format_digest(clusters, conn), disable_web_page_preview=False)


def discover_chat(token: str) -> int:
    resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    resp.raise_for_status()
    data = resp.json()
    updates = data.get("result", [])
    if not updates:
        print("هنوز چتی پیدا نشد. اول در تلگرام دستور /start را برای بات بفرستید، بعد دوباره این دستور را اجرا کنید.")
        return 1
    for update in updates:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat", {})
        if chat:
            print(f"{chat.get('id')}  {chat.get('type')}  {chat.get('title') or chat.get('username') or chat.get('first_name')}")
    return 0


SOCIAL_URL_RE = re.compile(r"https?://(?:www\.)?(?:instagram\.com|tiktok\.com|vt\.tiktok\.com|x\.com|twitter\.com)/\S+", re.I)


def extract_social_urls(text: str) -> list[str]:
    urls = []
    for match in SOCIAL_URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(").,]")
        if url not in urls:
            urls.append(url)
    return urls


def save_social_links(conn: sqlite3.Connection, urls: list[str], note: str, user_id: str | None, chat_id: str | None) -> int:
    saved = 0
    for url in urls:
        try:
            conn.execute(
                "INSERT INTO saved_links (url, note, user_id, chat_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (url, clean_text(note, 500), user_id, chat_id, utcnow().isoformat()),
            )
            saved += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    return saved


def source_status(config: dict[str, Any]) -> str:
    lines = ["<b>منابع فعال</b>"]
    for source in config.get("sources", []):
        status = "فعال" if source.get("enabled") is not False else "غیرفعال"
        mode = "صفحه" if source.get("type") == "page" else "فید"
        name = farsi_source_name(source.get("name", "منبع نامشخص"))
        lines.append(f"{html.escape(name)}: {status}، {mode}")
    return "\n".join(lines)


def saved_links_text(conn: sqlite3.Connection, limit: int = 10) -> str:
    rows = conn.execute(
        "SELECT id, url, note, created_at FROM saved_links ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        return "هنوز لید اجتماعی ذخیره نشده است. لینک اینستاگرام، تیک تاک یا ایکس را برای بات فوروارد کنید."
    lines = ["<b>لیدهای اجتماعی ذخیره شده</b>"]
    for row in rows:
        note = clean_text(row[2] or "", 90)
        lines.append(f"{row[0]}. <a href=\"{html.escape(row[1])}\">{html.escape(urlparse(row[1]).netloc)}</a> {html.escape(note)}")
    return "\n".join(lines)


def delete_saved_link(conn: sqlite3.Connection, text: str) -> str:
    match = re.search(r"/(?:delete|unsave)\s+(?:saved\s+)?(\d+)", text, re.I)
    if not match:
        return "برای حذف، مثلا بنویسید: /delete saved 3"
    cur = conn.execute("DELETE FROM saved_links WHERE id = ?", (int(match.group(1)),))
    conn.commit()
    return "لید ذخیره شده حذف شد." if cur.rowcount else "این لید پیدا نشد."


def trend_lines(clusters: list[StoryCluster], limit: int = 8) -> list[str]:
    trends = [cluster for cluster in clusters if len(cluster.sources) > 1]
    trends = sorted(trends, key=lambda item: (len(item.sources), item.score), reverse=True)[:limit]
    if not trends:
        return ["فعلا ترند چندمنبعی پیدا نشد."]
    lines = []
    for idx, cluster in enumerate(trends, 1):
        title, _, _ = farsi_title_and_summary(cluster)
        lines.append(
            f"{farsi_digits(str(idx))}. {title} "
            f"({farsi_digits(str(len(cluster.sources)))} منبع: {farsi_sources_line(cluster, 3)})"
        )
    return lines


def content_calendar_lines(clusters: list[StoryCluster], conn: sqlite3.Connection | None = None, limit: int = 6) -> list[str]:
    lines = []
    for idx, cluster in enumerate(clusters[:limit], 1):
        title, _, caption = farsi_title_and_summary(cluster)
        lines.append(f"{farsi_digits(str(idx))}. {title} - {caption}")
    return lines or ["فعلا پیشنهادی برای تقویم محتوا پیدا نشد."]


def format_daily_report(clusters: list[StoryCluster], conn: sqlite3.Connection | None = None) -> str:
    watch_terms = list_watch_terms(conn) if conn else []
    lines = [
        "<b>گزارش روزانه رادار دبی</b>",
        "",
        "<b>خبرهای مهم</b>",
    ]
    for idx, cluster in enumerate(clusters[:8], 1):
        editorial = ai_editorial_package(conn, cluster)
        title, _, _ = farsi_title_and_summary(cluster)
        lines.append(f"{farsi_digits(str(idx))}. {html.escape(title)}")
        lines.append(html.escape(editorial["farsi"]))
        lines.append("<b>پک کپشن فارسی:</b>\n" + html.escape(editorial["persian_social"]))
        lines.append("<b>آماده کپی برای پست:</b>\n" + html.escape(editorial["copy_ready"]))
    lines.extend(["", "<b>ترندها</b>"])
    lines.extend(html.escape(line) for line in trend_lines(clusters))
    lines.extend(["", "<b>تقویم محتوا</b>"])
    lines.extend(html.escape(line) for line in content_calendar_lines(clusters, conn))
    lines.extend(["", "<b>واچ لیست</b>", html.escape("، ".join(watch_terms) if watch_terms else "فعلا موردی ثبت نشده است.")])
    return "\n".join(lines)


def format_heartbeat(clusters: list[StoryCluster], candidates_count: int, conn: sqlite3.Connection | None = None) -> str:
    unseen = [cluster for cluster in clusters if not (seen_cluster(conn, cluster) if conn else False)]
    lines = [
        "<b>وضعیت رادار مجله دبی</b>",
        "بات فعال است و امروز منابع را بررسی کرده است.",
        f"خبرهای بررسی شده: {farsi_digits(str(candidates_count))}",
        f"گروه های خبری: {farsi_digits(str(len(clusters)))}",
        f"گروه های جدید ارسال نشده: {farsi_digits(str(len(unseen)))}",
        "",
        "<b>سیگنال های مهم</b>",
    ]
    for idx, cluster in enumerate(clusters[:5], 1):
        title, _, _ = farsi_title_and_summary(cluster)
        lines.append(f"{farsi_digits(str(idx))}. {html.escape(title)}")
    if not clusters:
        lines.append("در بازه فعلی خبر قوی پیدا نشد.")
    lines.extend(["", "هشدارها فقط وقتی ارسال می شوند که خبر از حد امتیاز لازم عبور کند."])
    return "\n".join(lines)


def help_text() -> str:
    return "\n".join(
        [
            "<b>راهنمای رادار مجله دبی</b>",
            "",
            "/help - نمایش همین راهنما",
            "/status - وضعیت لیدهای ذخیره شده و بازخوردها",
            "/sources - نمایش منابع فعال خبر",
            "/saved - مرور لینک های ذخیره شده از اینستاگرام، تیک تاک یا ایکس",
            "/delete saved 3 - حذف یک لینک ذخیره شده",
            "/today - پنج خبر آماده پست با کپشن فارسی و پرامپت تصویر",
            "/digest - ارسال خلاصه خبرهای مهم فعلی",
            "/digest lifestyle - رستوران، رویداد، مال، پاپ آپ و ایده آخر هفته",
            "/digest viral - خبرهای وایرال و مناسب شبکه اجتماعی",
            "/digest crime - پلیس، دادگاه، کلاهبرداری، دستگیری و امنیت عمومی",
            "/digest rules - ویزا، جریمه، مجوز، سالک، پارکینگ و مترو",
            "/digest weather - آب وهوا، ترافیک، جاده و هشدارهای روزانه",
            "/digest business - استارتاپ، ملک، سرمایه گذاری و اقتصاد",
            "/trends - خبرهایی که چند منبع پوشش داده اند",
            "/report - گزارش روزانه برای صفحه مجله",
            "/calendar - پیشنهاد تقویم محتوایی",
            "/watch rents - اضافه کردن یک کلمه به واچ لیست",
            "/watchlist - نمایش واچ لیست",
            "/unwatch rents - حذف یک کلمه از واچ لیست",
            "",
            "اگر لینک اینستاگرام، تیک تاک یا ایکس را فوروارد کنید، بات آن را به عنوان لید اجتماعی ذخیره می کند.",
            "با دکمه های مفید، کم اهمیت، دیر و بیشتر، رتبه بندی بات بهتر می شود.",
            "هر خبر لینک منبع دارد؛ خبرهای خوشه ای می توانند تا چهار لینک منبع داشته باشند.",
            "اگر تصویر مقاله پیدا شود، قبل از متن کامل خبر ارسال می شود.",
            "هر خبر شامل عنوان فارسی، خلاصه کامل فارسی، کپشن کوتاه، پک کپشن فارسی، پرامپت تصویر و متن آماده کپی برای پست است.",
            "",
            "ارسال خودکار:",
            "هشدارها فقط وقتی ارسال می شوند که خبر از حد امتیاز لازم عبور کند.",
            "دایجست و گزارش روزانه خبرها، ترندها و پیشنهادهای محتوایی را جمع می کنند.",
        ]
    )


def process_updates(
    token: str,
    conn: sqlite3.Connection,
    config: dict[str, Any],
    hours: int,
    min_score: int,
    limit: int,
) -> int:
    offset_raw = state_get(conn, "telegram_update_offset")
    params = {"timeout": 0}
    if offset_raw:
        params["offset"] = int(offset_raw)
    resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=20)
    resp.raise_for_status()
    updates = resp.json().get("result", [])
    processed = 0

    for update in updates:
        processed += 1
        state_set(conn, "telegram_update_offset", str(int(update["update_id"]) + 1))

        callback = update.get("callback_query")
        if callback:
            data = callback.get("data", "")
            parts = data.split(":")
            if len(parts) == 3 and parts[0] == "fb":
                user = callback.get("from", {})
                save_feedback(conn, parts[1], parts[2], str(user.get("id")) if user.get("id") else None)
                telegram_call(
                    token,
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback["id"],
                        "text": "ثبت شد. رادار از این بازخورد یاد می گیرد.",
                        "show_alert": False,
                    },
                )
            elif len(parts) == 3 and parts[0] == "act":
                user = callback.get("from", {})
                save_approval(conn, parts[1], parts[2], str(user.get("id")) if user.get("id") else None)
                telegram_call(
                    token,
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback["id"],
                        "text": "ثبت شد.",
                        "show_alert": False,
                    },
                )
            continue

        message = update.get("message") or update.get("channel_post") or {}
        text = message.get("text") or message.get("caption") or ""
        chat = message.get("chat", {})
        user = message.get("from", {})
        chat_id = str(chat.get("id")) if chat.get("id") else None
        user_id = str(user.get("id")) if user.get("id") else None

        if (text.startswith("/help") or text.startswith("/start")) and chat_id:
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": help_text(), "parse_mode": "HTML"})
            continue
        if text.startswith("/sources") and chat_id:
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": source_status(config), "parse_mode": "HTML"})
            continue
        if text.startswith("/saved") and chat_id:
            telegram_call(
                token,
                "sendMessage",
                {"chat_id": chat_id, "text": saved_links_text(conn), "parse_mode": "HTML", "disable_web_page_preview": True},
            )
            continue
        if (text.startswith("/delete") or text.startswith("/unsave")) and chat_id:
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": delete_saved_link(conn, text)})
            continue
        if text.startswith("/watchlist") and chat_id:
            terms = list_watch_terms(conn)
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": "واچ لیست: " + ("، ".join(terms) if terms else "خالی")})
            continue
        if text.startswith("/watch ") and chat_id:
            term = text.split(" ", 1)[1]
            add_watch_term(conn, term)
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": f"به واچ لیست اضافه شد: {clean_text(term, 80)}"})
            continue
        if text.startswith("/unwatch ") and chat_id:
            term = text.split(" ", 1)[1]
            removed = remove_watch_term(conn, term)
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": "حذف شد." if removed else "این کلمه در واچ لیست نبود."})
            continue
        if text.startswith("/today") and chat_id:
            clusters = apply_watch_boost(build_digest_clusters(config, hours, min_score, 20, None), list_watch_terms(conn))
            if not clusters:
                telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": "فعلا خبر آماده پست قوی پیدا نشد."})
                continue
            send_today(token, chat_id, clusters, conn)
            continue
        if text.startswith("/digest") and chat_id:
            category = digest_category_from_text(text)
            if category and category not in set(DIGEST_ALIASES.values()):
                telegram_call(
                    token,
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": "این دسته بندی را نمی شناسم. دستور /help را بزنید و یکی از دسته بندی های همان راهنما را انتخاب کنید.",
                    },
                )
                continue
            clusters = build_digest_clusters(config, hours, min_score, limit, category)
            clusters = apply_watch_boost(clusters, list_watch_terms(conn))
            if not clusters:
                telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": "فعلا خبری در این دسته پیدا نشد."})
                continue
            telegram_call(
                token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": format_digest(clusters, conn),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
            )
            continue
        if text.startswith("/trends") and chat_id:
            clusters = apply_watch_boost(build_digest_clusters(config, hours, min_score, 40, None), list_watch_terms(conn))
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": "<b>ترندها</b>\n" + "\n".join(html.escape(line) for line in trend_lines(clusters)), "parse_mode": "HTML"})
            continue
        if text.startswith("/calendar") and chat_id:
            clusters = apply_watch_boost(build_digest_clusters(config, hours, min_score, 20, None), list_watch_terms(conn))
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": "<b>تقویم محتوا</b>\n" + "\n".join(html.escape(line) for line in content_calendar_lines(clusters, conn)), "parse_mode": "HTML"})
            continue
        if text.startswith("/report") and chat_id:
            clusters = apply_watch_boost(build_digest_clusters(config, hours, min_score, 40, None), list_watch_terms(conn))
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": format_daily_report(clusters, conn), "parse_mode": "HTML", "disable_web_page_preview": True})
            continue
        if text.startswith("/status") and chat_id:
            saved_count = conn.execute("SELECT COUNT(*) FROM saved_links").fetchone()[0]
            feedback_count = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
            approval_count = conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
            telegram_call(
                token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": (
                        "رادار فعال است.\n"
                        f"لیدهای اجتماعی ذخیره شده: {farsi_digits(str(saved_count))}\n"
                        f"بازخوردها: {farsi_digits(str(feedback_count))}\n"
                        f"اقدام های تایید/رد: {farsi_digits(str(approval_count))}"
                    ),
                },
            )
            continue

        urls = extract_social_urls(text)
        if urls and chat_id:
            saved = save_social_links(conn, urls, text, user_id, chat_id)
            reply = f"{farsi_digits(str(saved))} لید اجتماعی ذخیره شد." if saved else "این لید قبلا ذخیره شده بود."
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": reply})

    return processed


def main() -> int:
    parser = argparse.ArgumentParser(description="رادار خبری تلگرام برای مجله دبی")
    parser.add_argument("--config", default=os.getenv("NEWS_CONFIG", DEFAULT_CONFIG))
    parser.add_argument("--db", default=os.getenv("DB_PATH", DEFAULT_DB))
    parser.add_argument("--hours", type=int, default=int(os.getenv("LOOKBACK_HOURS", "24")))
    parser.add_argument("--limit", type=int, default=int(os.getenv("MAX_ITEMS", "8")))
    parser.add_argument("--min-score", type=int, default=int(os.getenv("MIN_SCORE", "7")))
    parser.add_argument("--breaking-score", type=int, default=int(os.getenv("BREAKING_SCORE", "14")))
    parser.add_argument("--mode", choices=["breaking", "digest", "report", "heartbeat", "all"], default=os.getenv("BOT_MODE", "breaking"))
    parser.add_argument("--category", default=os.getenv("DIGEST_CATEGORY"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--discover-chat", action="store_true")
    parser.add_argument("--process-updates", action="store_true")
    args = parser.parse_args()
    if args.category:
        raw_category = args.category.strip().lower().replace("-", "").replace("_", "")
        args.category = DIGEST_ALIASES.get(raw_category, raw_category)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if args.discover_chat:
        if not token:
            print("اول TELEGRAM_BOT_TOKEN را تنظیم کنید.", file=sys.stderr)
            return 2
        return discover_chat(token)

    config = load_config(args.config)
    conn = init_db(args.db)
    if args.process_updates:
        if not token:
            print("اول TELEGRAM_BOT_TOKEN را تنظیم کنید.", file=sys.stderr)
            return 2
        processed = process_updates(token, conn, config, args.hours, args.min_score, args.limit)
        print(f"{processed} به روزرسانی تلگرام پردازش شد.")
        return 0

    candidates = [story for story in collect(config, args.hours) if story.score >= args.min_score]
    clusters = build_clusters(candidates)
    clusters = apply_watch_boost(clusters, list_watch_terms(conn))
    clusters = filter_clusters_by_category(clusters, args.category)
    if args.mode == "breaking":
        clusters = [cluster for cluster in clusters if cluster.score >= args.breaking_score]
    fresh = [cluster for cluster in clusters if not seen_cluster(conn, cluster)][: args.limit]

    if args.dry_run:
        if args.mode == "heartbeat":
            print(format_heartbeat(clusters, len(candidates), conn))
            return 0
        for cluster in fresh:
            editorial = ai_editorial_package(conn, cluster)
            title, _, _ = farsi_title_and_summary(cluster)
            print(f"[{cluster.score}] {title}")
            print(f"    منبع: {farsi_sources_line(cluster)}")
            print(f"    {editorial['farsi']}")
            print(f"    پک کپشن فارسی: {editorial['persian_social']}")
            print(f"    آماده کپی برای پست: {editorial['copy_ready']}")
            for link in cluster.links[:4]:
                print(f"    {link}")
        print(f"{len(fresh)} گروه قابل ارسال از {len(candidates)} خبر و {len(clusters)} گروه خبری.")
        return 0

    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID را تنظیم کنید یا با --dry-run اجرا کنید.", file=sys.stderr)
        return 2

    if args.mode == "heartbeat":
        telegram_call(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": format_heartbeat(clusters, len(candidates), conn),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        print(f"پیام وضعیت با {len(clusters)} گروه خبری ارسال شد.")
        return 0

    if args.mode == "digest":
        if fresh:
            send_digest(token, chat_id, fresh, conn)
            for cluster in fresh:
                mark_seen(conn, cluster)
        print(f"دایجست با {len(fresh)} گروه خبری ارسال شد.")
        return 0

    if args.mode == "report":
        if fresh:
            send_html_message(token, chat_id, format_daily_report(fresh, conn), disable_web_page_preview=True)
        print(f"گزارش روزانه با {len(fresh)} گروه خبری ارسال شد.")
        return 0

    for cluster in fresh:
        send_cluster(token, chat_id, cluster, conn)
        mark_seen(conn, cluster)
    print(f"{len(fresh)} گروه خبری ارسال شد.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
