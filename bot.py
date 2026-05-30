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
        ("lifestyle", r"restaurant|brunch|hotel|mall|pop-up|popup|concert|festival|weekend|eid|karak|فعالية|مهرجان"),
        ("rules", r"visa|fine|law|rule|permit|salik|parking|metro|rta|تأشيرة|غرامة"),
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
        base = f"{cluster.title}. Multiple UAE outlets are covering this developing story."
    return base


FARSI_SOURCE_NAMES = {
    "ARN News Centre UAE": "ای آر ان نیوز",
    "Barq UAE Arabic": "برق امارات",
    "Dubai One / Emirates 24|7 UAE": "دبی وان / امارات ۲۴/۷",
    "Gulf News UAE": "گلف نیوز",
    "Khaleej Times UAE": "خلیج تایمز",
    "Lovin Dubai": "لاوین دبی",
    "The National UAE": "نشنال",
}


def farsi_source_name(source: str) -> str:
    return FARSI_SOURCE_NAMES.get(source, source)


def farsi_digits(value: str) -> str:
    return value.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def farsi_money_detail(text: str) -> str:
    match = re.search(r"(?:aed|dh|dirham|درهم)\s*([0-9][0-9,]*)|([0-9][0-9,]*)\s*(?:aed|dh|dirham|درهم)", text, re.I)
    if not match:
        if re.search(r"100\s*ألف|100\s*الف|۱۰۰\s*هزار", text, re.I):
            return "۱۰۰ هزار درهم"
        return "مبلغ قابل توجهی"
    amount = (match.group(1) or match.group(2) or "").replace(",", "")
    return f"{farsi_digits(amount)} درهم" if amount else "مبلغ قابل توجهی"


def farsi_brief(cluster: StoryCluster) -> str:
    raw_text = f"{cluster.title} {cluster.best_story.summary}"
    text = raw_text.lower()
    tags = set(cluster.tags)
    source = cluster.sources[0] if cluster.sources else cluster.best_story.source
    source_text = farsi_source_name(source)
    coverage = "چند رسانه معتبر همزمان این موضوع را پوشش داده اند، پس ارزش توجه بیشتری دارد. " if len(cluster.sources) > 1 else ""
    if re.search(r"fake|fraud|scam|warn|booking|کلاهبرداری|احتيال|مزيف", text, re.I):
        if re.search(r"travel|holiday|booking|flight|hotel|summer|سفر|هتل|پرواز", text, re.I):
            main = "پلیس دبی درباره رزروهای جعلی سفر، بلیت و هتل هشدار داده است؛ مخصوصا پیشنهادهایی که با تخفیف های وسوسه کننده منتشر می شوند."
            why = "نکته مهم این است که مخاطب قبل از پرداخت باید آدرس سایت، صفحه شبکه اجتماعی و واقعی بودن شرکت را بررسی کند."
        else:
            main = "مقام های رسمی درباره یک روش تازه کلاهبرداری یا پیشنهاد جعلی هشدار داده اند که می تواند برای ساکنان دبی دردسرساز شود."
            why = "نکته مهم این است که مخاطب قبل از پرداخت، ارسال اطلاعات شخصی یا کلیک روی لینک ها باید چند نشانه اعتماد را بررسی کند."
        angle = "ایده کپشن: با یک جمله هشداردهنده شروع کنید و بعد خیلی کوتاه بگویید مردم چطور از خودشان محافظت کنند."
    elif re.search(r"(return|returned|found|honesty|أمانت|عثر|سلم|سلّم)", text, re.I) and re.search(
        r"\baed\b|\bdh\b|dirham|درهم|100,?000|100 ألف", text, re.I
    ):
        amount = farsi_money_detail(raw_text)
        main = f"این یک خبر مثبت محلی از دبی است؛ فردی {amount} یا یک مال گمشده را پیدا کرده و آن را به پلیس یا صاحبش برگردانده است."
        why = "نکته مهم، تصویر انسانی و قابل اشتراک گذاری از امانت داری و اعتماد اجتماعی در دبی است."
        angle = "ایده کپشن: حس خوب شهر، رفتار درست شهروندی و واکنش پلیس یا مردم را برجسته کنید."
    elif re.search(r"solidarity|condolence|condemns|foreign|minister|تعزي|تتضامن|يدين", text, re.I):
        if re.search(r"kenya|fire|school|dormitory|کنیا|حریق|آتش", text, re.I):
            main = "امارات پس از آتش سوزی مرگبار در یک خوابگاه دختران در کنیا، پیام همبستگی و تسلیت منتشر کرده است."
            why = "نکته مهم، جنبه انسانی حادثه و موضع رسمی امارات در همدردی با قربانیان و خانواده هاست."
        else:
            main = "امارات در واکنش به یک اتفاق مهم بین المللی پیام همبستگی، تسلیت یا موضع رسمی منتشر کرده است."
            why = "نکته مهم، نقش دیپلماسی امارات و پیام انسانی این واکنش است."
        angle = "ایده کپشن: خبر را با تمرکز بر همدردی، احترام و بعد انسانی ماجرا روایت کنید."
    elif "crime" in tags:
        main = "این خبر به پلیس، دادگاه، امنیت عمومی، پرونده های قضایی یا هشدارهایی مربوط است که می تواند برای ساکنان دبی مهم باشد."
        why = "نکته مهم این است که خبر فقط اطلاع رسانی نیست؛ ممکن است روی امنیت، رفتار روزمره یا تصمیم های مردم اثر بگذارد."
        angle = "ایده کپشن: اول اثر مستقیم خبر روی مردم را بگویید و بعد نکته احتیاطی یا قانونی را واضح اضافه کنید."
    elif "rules" in tags:
        if re.search(r"eswatini|إسواتيني|اسواتيني|visa waiver|mutual visa|الإعفاء المتبادل|تأشيرة الدخول", text, re.I):
            main = "امارات و پادشاهی اسواتینی تفاهم نامه ای برای معافیت متقابل از شرط ویزای ورود امضا کرده اند."
            why = "نکته مهم این است که چنین توافق هایی می توانند رفت وآمد، سفرهای کاری و روابط رسمی بین دو کشور را ساده تر کنند."
            angle = "ایده کپشن: خبر را به زبان ساده توضیح دهید؛ چه کشورهایی درگیرند، موضوع ویزا چیست و چرا برای سفر یا روابط بین المللی مهم است."
        else:
            main = "این خبر درباره یک تغییر یا یادآوری کاربردی در قوانین، ویزا، جریمه ها، مجوزها، پارکینگ، سالک یا خدمات شهری امارات است."
            why = "نکته مهم این است که مخاطب بداند چه چیزی تغییر کرده و آیا این موضوع شامل حال او می شود یا نه."
            angle = "ایده کپشن: خبر را مثل یک راهنمای سریع بنویسید؛ چه اتفاقی افتاده، از چه زمانی مهم است و چه کسانی باید حواسشان باشد."
    elif "weather/traffic" in tags:
        main = "این خبر یک اطلاع رسانی روزمره و کاربردی درباره آب وهوا، جاده ها، ترافیک، پروازها یا رفت وآمد در دبی و امارات است."
        why = "نکته مهم، کمک به برنامه ریزی روزانه مخاطب است؛ مخصوصا اگر روی مسیر، زمان حرکت یا برنامه سفر اثر بگذارد."
        angle = "ایده کپشن: زمان، مکان و کاری را که مخاطب باید انجام دهد در همان خط اول بیاورید."
    elif "lifestyle" in tags:
        main = "این خبر برای محتوای سبک زندگی دبی مناسب است؛ از رویداد و رستوران گرفته تا مراکز خرید، پاپ آپ ها و برنامه های آخر هفته."
        why = "نکته مهم این است که خبر می تواند برای مخاطب به یک پیشنهاد قابل انجام تبدیل شود، نه فقط یک اطلاعیه ساده."
        angle = "ایده کپشن: آن را مثل یک پیشنهاد کوتاه و جذاب برای تجربه کردن در دبی معرفی کنید."
    elif "viral" in tags:
        main = "این موضوع ظرفیت وایرال شدن دارد، چون می تواند بحث راه بیندازد، تصویر خوبی بسازد یا برای مخاطب قابل اشتراک گذاری باشد."
        why = "نکته مهم، زاویه احساسی یا گفت وگومحور خبر است؛ چیزی که باعث می شود مردم نظر بدهند یا آن را برای دیگران بفرستند."
        angle = "ایده کپشن: با یک سوال کوتاه، جمله کنجکاوکننده یا زاویه بحث برانگیز شروع کنید تا کامنت بگیرد."
    elif "business" in tags:
        main = "این خبر به اقتصاد دبی، بازار ملک، سرمایه گذاری، استارتاپ ها، فرصت های شغلی یا فضای کسب وکار مربوط است."
        why = "نکته مهم این است که مخاطب بفهمد این خبر چه اثری روی هزینه زندگی، فرصت ها، ملک یا تصمیم های مالی دارد."
        angle = "ایده کپشن: اثر خبر روی زندگی، پول، ملک یا تصمیم های روزمره مردم را ساده و قابل لمس توضیح دهید."
    else:
        main = "این یک به روزرسانی تازه درباره دبی یا امارات است که می تواند برای مخاطب محلی ارزش خبری یا کاربردی داشته باشد."
        why = "نکته مهم این است که خبر به زندگی روزمره، تصویر شهر یا گفت وگوهای امروز مردم ربط پیدا می کند."
        angle = "ایده کپشن: یک برداشت کوتاه و روشن از اهمیت خبر بدهید و بگویید چرا مخاطب باید آن را بداند."
    return f"خلاصه فارسی: {coverage}اصل خبر: {main} {why} {angle} منبع: {source_text}."


def fallback_editorial_package(cluster: StoryCluster) -> dict[str, str]:
    summary = caption_summary(cluster)
    idea = post_suggestion(cluster)
    return {
        "headline": clean_text(cluster.title, 120),
        "caption": summary,
        "farsi": farsi_brief(cluster),
        "post_idea": idea,
        "carousel_title": clean_text(cluster.title, 70),
        "why_care": why_care(cluster),
        "calendar_slot": calendar_slot(cluster),
    }


def ai_editorial_package(conn: sqlite3.Connection | None, cluster: StoryCluster) -> dict[str, str]:
    return fallback_editorial_package(cluster)


def post_suggestion(cluster: StoryCluster) -> str:
    title = cluster.title.rstrip(".")
    text = f"{cluster.title} {cluster.best_story.summary}".lower()
    tags = set(cluster.tags)
    if re.search(r"solidarity|condolence|condemns|foreign|minister|تعزي|تتضامن|يدين", text, re.I):
        return "Post angle: Use this as a short UAE diplomacy update with the human impact first."
    if re.search(r"(return|returned|found|honesty|أمانت|عثر|سلم|سلّم)", text, re.I) and re.search(
        r"\baed\b|\bdh\b|dirham|درهم|100,?000|100 ألف", text, re.I
    ):
        return "Post angle: Frame it as a feel-good Dubai honesty story with a strong local hook."
    if re.search(r"fake|fraud|scam|warn", text, re.I):
        return "Post angle: Turn this into a practical warning post with the red flags people should check."
    if "breaking" in tags:
        return f"Post angle: Lead with what happened, where it happened, and what residents should do next."
    if "viral" in tags:
        return f"Post angle: Why this Dubai moment is getting people talking today."
    if "lifestyle" in tags:
        return f"Post angle: Add this to the Dubai weekend radar."
    if "rules" in tags:
        return f"Post angle: Explain the practical change and who it affects in Dubai."
    if "weather/traffic" in tags:
        return f"Post angle: A quick resident advisory with the key timing and location."
    if "business" in tags:
        return f"Post angle: Frame this as a Dubai business trend worth watching."
    return f"Post angle: Turn this into a short Dubai update with one clear takeaway."


def why_care(cluster: StoryCluster) -> str:
    tags = set(cluster.tags)
    if "crime" in tags:
        return "Useful for residents because it points to safety, scams, or legal risk."
    if "viral" in tags:
        return "Good for engagement because the story has social conversation potential."
    if "lifestyle" in tags:
        return "Useful for weekend planning and quick audience-friendly content."
    if "rules" in tags:
        return "Practical because it affects how people move, travel, pay, or comply."
    if "weather/traffic" in tags:
        return "Timely because it helps residents plan their day."
    if "business" in tags:
        return "Useful as a Dubai economy or startup trend signal."
    return "A timely Dubai update with clear audience relevance."


def calendar_slot(cluster: StoryCluster) -> str:
    tags = set(cluster.tags)
    if "breaking" in tags or "weather/traffic" in tags:
        return "Post today as a quick update."
    if "viral" in tags:
        return "Post today as a Reel or short social caption."
    if "lifestyle" in tags:
        return "Save for weekend roundup or carousel."
    if "business" in tags or "rules" in tags:
        return "Use tomorrow as an explainer carousel."
    return "Save for the daily roundup."


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
            geo_terms = config.get("require_any_terms", [])
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


def feedback_keyboard(cluster: StoryCluster) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Useful", "callback_data": f"fb:{cluster.key}:useful"},
                {"text": "Boring", "callback_data": f"fb:{cluster.key}:boring"},
                {"text": "Too late", "callback_data": f"fb:{cluster.key}:late"},
                {"text": "More", "callback_data": f"fb:{cluster.key}:more"},
            ],
            [
                {"text": "Approve", "callback_data": f"act:{cluster.key}:approve"},
                {"text": "Skip", "callback_data": f"act:{cluster.key}:skip"},
                {"text": "Rewrite", "callback_data": f"act:{cluster.key}:rewrite"},
                {"text": "Later", "callback_data": f"act:{cluster.key}:later"},
            ]
        ]
    }


def format_cluster(cluster: StoryCluster, conn: sqlite3.Connection | None = None) -> str:
    best = cluster.best_story
    reasons = ", ".join(cluster.reasons) if cluster.reasons else "new"
    tags = ", ".join(cluster.tags)
    summary = f"\n\n{html.escape(best.summary)}" if best.summary else ""
    editorial = ai_editorial_package(conn, cluster)
    source_line = ", ".join(cluster.sources)
    links = "\n".join(
        f"{idx + 1}. <a href=\"{html.escape(link)}\">{html.escape(urlparse(link).netloc)}</a>"
        for idx, link in enumerate(cluster.links[:4])
    )
    return (
        f"<b>{html.escape(cluster.title)}</b>\n"
        f"{html.escape(source_line)} | score {cluster.score} | {html.escape(tags)}\n"
        f"{html.escape(reasons)}"
        f"{summary}\n\n"
        f"<b>Caption:</b> {html.escape(editorial['caption'])}\n\n"
        f"<b>Farsi:</b> {html.escape(editorial['farsi'])}\n\n"
        f"<b>Post idea:</b> {html.escape(editorial['post_idea'])}\n"
        f"<b>Why care:</b> {html.escape(editorial['why_care'])}\n"
        f"<b>Calendar:</b> {html.escape(editorial['calendar_slot'])}\n\n"
        f"<b>Image:</b> {html.escape(cluster_image_url(cluster) or 'No image found')}\n\n"
        f"{links}"
    )


def format_digest(clusters: list[StoryCluster], conn: sqlite3.Connection | None = None) -> str:
    lines = [
        "<b>Dubai Magazine Radar</b>",
        f"{len(clusters)} strongest stories found",
        "",
    ]
    for idx, cluster in enumerate(clusters, 1):
        best = cluster.best_story
        editorial = ai_editorial_package(conn, cluster)
        source_line = ", ".join(cluster.sources[:3])
        tags = ", ".join(cluster.tags[:3])
        lines.extend(
            [
                f"<b>{idx}. {html.escape(editorial['headline'])}</b>",
                f"{html.escape(source_line)} | score {cluster.score} | {html.escape(tags)}",
                f"Caption: {html.escape(editorial['caption'])}",
                f"Farsi: {html.escape(editorial['farsi'])}",
                f"Idea: {html.escape(editorial['post_idea'])}",
                f"Calendar: {html.escape(editorial['calendar_slot'])}",
                f"<a href=\"{html.escape(best.link)}\">Open lead source</a>",
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
                    "caption": html.escape(clean_text(cluster.title, 900)),
                    "parse_mode": "HTML",
                },
            )
        except Exception:
            pass
    telegram_call(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": format_cluster(cluster, conn),
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
            "reply_markup": feedback_keyboard(cluster),
        },
    )


def send_digest(token: str, chat_id: str, clusters: list[StoryCluster], conn: sqlite3.Connection | None = None) -> None:
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


def discover_chat(token: str) -> int:
    resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    resp.raise_for_status()
    data = resp.json()
    updates = data.get("result", [])
    if not updates:
        print("No chats found yet. Send /start to the bot in Telegram, then run this again.")
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
    lines = ["<b>Sources</b>"]
    for source in config.get("sources", []):
        status = "on" if source.get("enabled") is not False else "off"
        mode = source.get("type", "rss")
        lines.append(f"{html.escape(source.get('name', 'Unknown'))}: {status}, {mode}")
    return "\n".join(lines)


def saved_links_text(conn: sqlite3.Connection, limit: int = 10) -> str:
    rows = conn.execute(
        "SELECT id, url, note, created_at FROM saved_links ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        return "No saved social leads yet. Forward an Instagram, TikTok, or X link to save one."
    lines = ["<b>Saved Social Leads</b>"]
    for row in rows:
        note = clean_text(row[2] or "", 90)
        lines.append(f"{row[0]}. <a href=\"{html.escape(row[1])}\">{html.escape(urlparse(row[1]).netloc)}</a> {html.escape(note)}")
    return "\n".join(lines)


def delete_saved_link(conn: sqlite3.Connection, text: str) -> str:
    match = re.search(r"/(?:delete|unsave)\s+(?:saved\s+)?(\d+)", text, re.I)
    if not match:
        return "Use /delete saved 3 to remove a saved social lead."
    cur = conn.execute("DELETE FROM saved_links WHERE id = ?", (int(match.group(1)),))
    conn.commit()
    return "Deleted saved lead." if cur.rowcount else "Could not find that saved lead."


def trend_lines(clusters: list[StoryCluster], limit: int = 8) -> list[str]:
    trends = [cluster for cluster in clusters if len(cluster.sources) > 1]
    trends = sorted(trends, key=lambda item: (len(item.sources), item.score), reverse=True)[:limit]
    if not trends:
        return ["No multi-source trends found yet."]
    return [
        f"{idx}. {cluster.title} ({len(cluster.sources)} sources: {', '.join(cluster.sources[:3])})"
        for idx, cluster in enumerate(trends, 1)
    ]


def content_calendar_lines(clusters: list[StoryCluster], conn: sqlite3.Connection | None = None, limit: int = 6) -> list[str]:
    lines = []
    for idx, cluster in enumerate(clusters[:limit], 1):
        editorial = ai_editorial_package(conn, cluster)
        lines.append(f"{idx}. {editorial['calendar_slot']} {editorial['carousel_title']}")
    return lines or ["No calendar suggestions available yet."]


def format_daily_report(clusters: list[StoryCluster], conn: sqlite3.Connection | None = None) -> str:
    watch_terms = list_watch_terms(conn) if conn else []
    lines = [
        "<b>Daily Dubai Intelligence Report</b>",
        "",
        "<b>Top Stories</b>",
    ]
    for idx, cluster in enumerate(clusters[:8], 1):
        editorial = ai_editorial_package(conn, cluster)
        lines.append(f"{idx}. {html.escape(editorial['headline'])} | {cluster.score} | {', '.join(cluster.tags[:3])}")
        lines.append(html.escape(editorial["farsi"]))
    lines.extend(["", "<b>Trend Signals</b>"])
    lines.extend(html.escape(line) for line in trend_lines(clusters))
    lines.extend(["", "<b>Content Calendar</b>"])
    lines.extend(html.escape(line) for line in content_calendar_lines(clusters, conn))
    lines.extend(["", "<b>Watchlist</b>", html.escape(", ".join(watch_terms) if watch_terms else "No watch terms yet.")])
    return "\n".join(lines)


def help_text() -> str:
    return "\n".join(
        [
            "<b>Dubai Magazine Radar Help</b>",
            "",
            "/help - Show this guide",
            "/status - Check saved leads and feedback count",
            "/sources - Show active news sources",
            "/saved - Review saved Instagram/TikTok/X leads",
            "/delete saved 3 - Remove a saved lead",
            "/digest - Send the current top digest",
            "/digest lifestyle - Restaurants, events, malls, pop-ups, weekend ideas",
            "/digest viral - Viral, social, watch/video, influencer-style stories",
            "/digest crime - Police, court, scams, arrests, public safety",
            "/digest rules - Visas, fines, permits, Salik, parking, metro",
            "/digest weather - Weather, traffic, roads, parking advisories",
            "/digest business - Startups, property, investment, economy",
            "/trends - Show stories covered by multiple sources",
            "/report - Daily intelligence report",
            "/calendar - Suggested content calendar",
            "/watch rents - Add a watchlist term",
            "/watchlist - Show watched terms",
            "/unwatch rents - Remove a watched term",
            "",
            "Forward an Instagram, TikTok, or X link and I will save it as a social lead.",
            "Tap Useful/Boring/Too late/More so ranking learns what is useful.",
            "Tap Approve/Skip/Rewrite/Later to manage editorial workflow.",
            "Every alert includes source links; clustered alerts can include up to four source links.",
            "When an article image is found, it is sent before the full alert.",
            "Every news item includes a short Farsi brief for quick caption planning.",
            "",
            "Alert types:",
            "Breaking alerts send high-score stories quickly.",
            "Daily digests and reports collect captions, post ideas, trends, and calendar suggestions.",
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
                        "text": "Saved. The radar will learn from this.",
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
                        "text": f"Marked: {parts[2]}",
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
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": "Watchlist: " + (", ".join(terms) if terms else "empty")})
            continue
        if text.startswith("/watch ") and chat_id:
            term = text.split(" ", 1)[1]
            add_watch_term(conn, term)
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": f"Watching: {clean_text(term, 80)}"})
            continue
        if text.startswith("/unwatch ") and chat_id:
            term = text.split(" ", 1)[1]
            removed = remove_watch_term(conn, term)
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": "Removed." if removed else "That term was not on the watchlist."})
            continue
        if text.startswith("/digest") and chat_id:
            category = digest_category_from_text(text)
            if category and category not in set(DIGEST_ALIASES.values()):
                telegram_call(
                    token,
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": "Unknown digest category. Try /digest lifestyle, /digest viral, /digest crime, /digest rules, /digest weather, or /digest business.",
                    },
                )
                continue
            clusters = build_digest_clusters(config, hours, min_score, limit, category)
            clusters = apply_watch_boost(clusters, list_watch_terms(conn))
            if not clusters:
                label = category or "top"
                telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": f"No {label} stories found right now."})
                continue
            title = f"<b>{html.escape((category or 'top').title())} Digest</b>\n\n"
            telegram_call(
                token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": title + format_digest(clusters, conn),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
            )
            continue
        if text.startswith("/trends") and chat_id:
            clusters = apply_watch_boost(build_digest_clusters(config, hours, min_score, 40, None), list_watch_terms(conn))
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": "<b>Trend Signals</b>\n" + "\n".join(html.escape(line) for line in trend_lines(clusters)), "parse_mode": "HTML"})
            continue
        if text.startswith("/calendar") and chat_id:
            clusters = apply_watch_boost(build_digest_clusters(config, hours, min_score, 20, None), list_watch_terms(conn))
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": "<b>Content Calendar</b>\n" + "\n".join(html.escape(line) for line in content_calendar_lines(clusters, conn)), "parse_mode": "HTML"})
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
                    "text": f"Radar is running.\nSaved social leads: {saved_count}\nFeedback clicks: {feedback_count}\nApproval actions: {approval_count}",
                },
            )
            continue

        urls = extract_social_urls(text)
        if urls and chat_id:
            saved = save_social_links(conn, urls, text, user_id, chat_id)
            reply = f"Saved {saved} social lead{'s' if saved != 1 else ''}." if saved else "Already saved this social lead."
            telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": reply})

    return processed


def main() -> int:
    parser = argparse.ArgumentParser(description="Dubai magazine Telegram news radar")
    parser.add_argument("--config", default=os.getenv("NEWS_CONFIG", DEFAULT_CONFIG))
    parser.add_argument("--db", default=os.getenv("DB_PATH", DEFAULT_DB))
    parser.add_argument("--hours", type=int, default=int(os.getenv("LOOKBACK_HOURS", "24")))
    parser.add_argument("--limit", type=int, default=int(os.getenv("MAX_ITEMS", "8")))
    parser.add_argument("--min-score", type=int, default=int(os.getenv("MIN_SCORE", "7")))
    parser.add_argument("--breaking-score", type=int, default=int(os.getenv("BREAKING_SCORE", "16")))
    parser.add_argument("--mode", choices=["breaking", "digest", "report", "all"], default=os.getenv("BOT_MODE", "breaking"))
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
            print("Set TELEGRAM_BOT_TOKEN first.", file=sys.stderr)
            return 2
        return discover_chat(token)

    config = load_config(args.config)
    conn = init_db(args.db)
    if args.process_updates:
        if not token:
            print("Set TELEGRAM_BOT_TOKEN first.", file=sys.stderr)
            return 2
        processed = process_updates(token, conn, config, args.hours, args.min_score, args.limit)
        print(f"Processed {processed} Telegram updates.")
        return 0

    candidates = [story for story in collect(config, args.hours) if story.score >= args.min_score]
    clusters = build_clusters(candidates)
    clusters = apply_watch_boost(clusters, list_watch_terms(conn))
    clusters = filter_clusters_by_category(clusters, args.category)
    if args.mode == "breaking":
        clusters = [cluster for cluster in clusters if cluster.score >= args.breaking_score]
    fresh = [cluster for cluster in clusters if not seen_cluster(conn, cluster)][: args.limit]

    if args.dry_run:
        for cluster in fresh:
            print(f"[{cluster.score}] {cluster.title}")
            print(f"    sources: {', '.join(cluster.sources)}")
            print(f"    tags: {', '.join(cluster.tags)}")
            print(f"    reasons: {', '.join(cluster.reasons)}")
            editorial = ai_editorial_package(conn, cluster)
            print(f"    caption: {editorial['caption']}")
            print(f"    farsi: {editorial['farsi']}")
            print(f"    idea: {editorial['post_idea']}")
            print(f"    why: {editorial['why_care']}")
            print(f"    calendar: {editorial['calendar_slot']}")
            print(f"    image: {cluster_image_url(cluster) or 'none'}")
            for link in cluster.links[:4]:
                print(f"    {link}")
        print(f"{len(fresh)} sendable clusters from {len(candidates)} stories and {len(clusters)} candidate clusters.")
        return 0

    if not token or not chat_id:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or run --dry-run.", file=sys.stderr)
        return 2

    if args.mode == "digest":
        if fresh:
            send_digest(token, chat_id, fresh, conn)
            for cluster in fresh:
                mark_seen(conn, cluster)
        print(f"Sent digest with {len(fresh)} clusters.")
        return 0

    if args.mode == "report":
        if fresh:
            telegram_call(
                token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": format_daily_report(fresh, conn),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
        print(f"Sent daily report with {len(fresh)} clusters.")
        return 0

    for cluster in fresh:
        send_cluster(token, chat_id, cluster, conn)
        mark_seen(conn, cluster)
    print(f"Sent {len(fresh)} clusters.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
