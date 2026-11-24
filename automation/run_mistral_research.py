"""Isolated Mistral-powered OSINT discovery, batch triage & safeguard workflow.

Reuses the existing GDELT candidate JSON schema without modifying
run_gemini_research.py, Gemini outputs, .env, or scheduled tasks.
Implements deterministic input provenance locking, 4-tier thesis relevance
guardrails, strict separation between page access verification and factual
claim verification, small-batch processing with raw batch preservation, full
zero-discard candidate accounting, cumulative cost limits with zero automatic
retries, and offline validation modes (--self-test, --replay-json, --dry-run).
All outputs are written separately to 02_Raw_Data/Mistral_Inbox/.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
AUTOMATION_DIR = ROOT / "automation"
MISTRAL_INBOX_DIR = ROOT / "02_Raw_Data" / "Mistral_Inbox"
PROMPT_FILE = ROOT / "00_Rules" / "free_tier_source_discovery_prompt.md"
LOG_DIR = AUTOMATION_DIR / "logs"
ENV_FILE = AUTOMATION_DIR / ".env"

TZ = ZoneInfo("Europe/Istanbul")
MISTRAL_ENDPOINT = "https://api.mistral.ai/v1/chat/completions"
MODEL_DEFAULT = "mistral-small-latest"
BATCH_SIZE_DEFAULT = 6
MAX_TOKENS_DEFAULT = 4500
MAX_TOTAL_COST_DEFAULT = 0.10
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
USER_AGENT = "Mozilla/5.0 (compatible; OSINTResearchStudent/1.0)"

MAX_PER_QUERY = 35
MAX_UNIQUE_CANDIDATES = 90
MAX_PAGES_TO_INSPECT = 60
PAGE_TEXT_LIMIT = 1800

FACTUAL_CLAIM_VERIFICATION_DEFAULT = "Pending Antigravity primary inspection"

# Published list pricing (USD per 1M tokens) for transparent cost estimation.
MODEL_PRICING_PER_M: dict[str, tuple[float, float]] = {
    "ministral-3b-latest": (0.10, 0.10),
    "ministral-8b-latest": (0.15, 0.15),
    "ministral-14b-latest": (0.20, 0.20),
    "mistral-small-latest": (0.15, 0.60),
}

ALLOWED_SOURCE_TYPES = {
    "News Article",
    "Opinion/Commentary",
    "Government/Official Document",
    "Academic Publication",
    "Think Tank Report",
}

ALLOWED_PAGE_ACCESS_STATUSES = {
    "Partially verified",
    "Not verified",
}

ALLOWED_RELEVANCE_TIERS = {
    "Direct",
    "Indirect",
    "Background",
    "Irrelevant",
}

RELEVANCE_TIER_LABELS_TR = {
    "Direct": "Doğrudan",
    "Indirect": "Dolaylı",
    "Background": "Arka plan",
    "Irrelevant": "İlgisiz",
}

REQUIRED_SUBSTANTIVE_FIELDS = (
    "candidate_id",
    "source_type",
    "content_summary",
    "thesis_relevance",
    "reliability_notes",
)

REQUIRED_SUBSTANTIVE_FIELDS_LEGACY = (
    "original_url",
    "source_type",
    "content_summary",
    "thesis_relevance",
    "reliability_notes",
)

REQUIRED_TOP_FIELDS = (
    "executive_summary",
    "records",
    "research_questions",
    "official_document_gap_note",
    "verification_priorities",
)

KNOWN_NON_OFFICIAL_DOMAINS = {
    "taz.de",
    "juancole.com",
    "444.hu",
    "kurierwilenski.lt",
    "news.day.az",
    "yle.fi",
    "vesti.ru",
    "glavcom.ua",
    "statuspress.com.ua",
    "delfi.ee",
    "delfi.lv",
    "delfi.lt",
    "lrt.lt",
    "lsm.lv",
    "err.ee",
    "tvnet.lv",
    "postimees.ee",
}

OFFICIAL_INSTITUTION_DOMAIN_RE = re.compile(
    r"(\.gov(\.|$)|\.mil(\.|$)|europa\.eu$|nato\.int$|osce\.org$|coe\.int$|un\.org$|"
    r"riigikogu\.ee$|riigiteataja\.ee$|eesti\.ee$|valitsus\.ee$|vm\.ee$|kul\.ee$|hm\.ee$|"
    r"saeima\.lv$|likumi\.lv$|president\.(ee|lv|lt)$|lrs\.lt$|parliament\.|"
    r"bundestag\.de$|congress\.gov$)",
    re.IGNORECASE,
)

ESTONIA_LATVIA_EVIDENCE_RE = re.compile(
    r"(estoni\w*|eesti\w*|tallinn\w*|narva\w*|ida-?viru\w*|riigikogu|estonya\w*|estland\w*|эстон\w*|таллин\w*|"
    r"latvi\w*|latvij\w*|riga\w*|riika\w*|daugavpils\w*|saeima|letonya\w*|lettland\w*|łotw\w*|латв\w*|рига\w*)",
    re.IGNORECASE,
)

DERUSSIFICATION_POLICY_EVIDENCE_RE = re.compile(
    r"(de-?russif\w*|entrussifizier\w*|russif\w*|de-?rusifikasyon|ruslaştırma|"
    r"russian[- ]?speak\w*|russophon\w*|russian minorit\w*|rusça konuş\w*|rus azınl\w*|"
    r"krievvalod\w*|venekeel\w*|vene keel\w*|русскоязыч\w*|дерусификац\w*|русификац\w*|"
    r"language (law|policy|reform|transition)|state language|official language|bilingual education|"
    r"russian[- ]?language school\w*|valsts valod\w*|riigikeel\w*|eestikeel\w*|latviešu valod\w*|"
    r"dil politik\w*|resm[iî] dil|eğitim reform\w*|okul dil\w*|"
    r"citizenship|non-citizen\w*|nepilsoņ\w*|stateless|residence permit\w*|vatandaşlık|vatansız|ikamet izn\w*|"
    r"russian media|broadcast ban|tv channel ban|media regulat\w*|yayın yasağ\w*|"
    r"soviet monument\w*|bronze soldier|pushkin|puschkin|denkm[aä]l\w*|памятник\w*|sovyet anıt\w*|heykel\w*|"
    r"orthodox church|moscow patriarchate|kilise yasa\w*|patrikhane|"
    r"domestic polit\w*|internal crisis response|iç siyaset\w*)",
    re.IGNORECASE,
)

COMPARATIVE_CONTEXT_EVIDENCE_RE = re.compile(
    r"(lithuan\w*|lietuv\w*|litvany\w*|litau\w*|lituani\w*|litw\w*|vilni\w*|wiln\w*|литв\w*|kaliningrad\w*|"
    r"baltic\w*|baltık\w*|bałty\w*|baltijos|de-?russif\w*|minorit\w*|azınl\w*|mniejszoś\w*)",
    re.IGNORECASE,
)

INACCESSIBLE_CLAIM_RE = re.compile(
    r"(sayfa içeriğine.*erişil|içeriğe.*erişil|otomatik erişilemedi|yalnızca gdelt başlığ|"
    r"sadece başlık mevcut|sayfa metni yok|sayfa açılmadı|content not inspected|"
    r"could not be accessed|inaccessible|headline[- ]only)",
    re.IGNORECASE,
)

SEARCHES = [
    ("Estonia", "(Estonia OR Eesti OR Estonian) (language OR education OR citizenship OR media OR minority OR parliament OR security OR defence OR defense OR election OR Russian)"),
    ("Latvia", "(Latvia OR Latvian OR Latvija) (language OR education OR citizenship OR media OR minority OR parliament OR security OR defence OR defense OR election OR Russian)"),
    ("Lithuania", "(Lithuania OR Lithuanian OR Lietuva) (parliament OR government OR defence OR defense OR security OR energy OR sanctions OR NATO OR Russia)"),
    ("Russia", "(Russia OR Russian) (Estonia OR Latvia OR Lithuania OR Baltic) (policy OR security OR NATO OR language OR minority OR disinformation OR sanctions)"),
    ("Ukraine", "(Ukraine OR Ukrainian) (Estonia OR Latvia OR Lithuania OR Baltic OR NATO) (war OR security OR defence OR defense OR assistance OR sanctions OR energy)"),
    ("Regional actors", '("Baltic states" OR "Baltic region") (NATO OR EU OR Belarus OR Poland OR Nordic OR Sweden OR Finland OR infrastructure OR security)'),
    ("De-Russification", '(derussification OR "de-Russification" OR "Russian language" OR Russification) (Estonia OR Latvia OR Lithuania)'),
]


def redact(text: str, secret: str) -> str:
    """Mask an API secret if it appears inside error diagnostics."""
    if secret and secret in text:
        return text.replace(secret, "[REDACTED]")
    return text


def slugify_model(model: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", model).strip("_").lower()


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
        return urlunparse(
            (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", "")
        )
    except Exception:
        return url.strip()


def setup_logging(mode_tag: str = "run") -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"mistral_{mode_tag}_{datetime.now(TZ):%Y-%m-%d_%H-%M-%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    return log_file


def output_path(prefix: str, run_date: str, suffix: str) -> Path:
    """Allocate a non-overwriting file path inside 02_Raw_Data/Mistral_Inbox."""
    MISTRAL_INBOX_DIR.mkdir(parents=True, exist_ok=True)
    path = MISTRAL_INBOX_DIR / f"{prefix}_{run_date}.{suffix}"
    if not path.exists():
        return path
    stamp = datetime.now(TZ).strftime("%H-%M-%S")
    candidate = MISTRAL_INBOX_DIR / f"{prefix}_{run_date}_rerun_{stamp}.{suffix}"
    counter = 1
    while candidate.exists():
        candidate = MISTRAL_INBOX_DIR / f"{prefix}_{run_date}_rerun_{stamp}_{counter}.{suffix}"
        counter += 1
    return candidate


def allocate_batch_dir(prefix: str, run_date: str) -> Path:
    stamp = datetime.now(TZ).strftime("%H-%M-%S")
    base = MISTRAL_INBOX_DIR / f"batches_{prefix}_{run_date}_{stamp}"
    counter = 1
    while base.exists():
        base = MISTRAL_INBOX_DIR / f"batches_{prefix}_{run_date}_{stamp}_{counter}"
        counter += 1
    base.mkdir(parents=True, exist_ok=True)
    return base


def date_window() -> tuple[datetime, datetime, str, str]:
    today = datetime.now(TZ).date()
    start_day = today - timedelta(days=6)
    start_local = datetime.combine(start_day, dt_time.min, tzinfo=TZ)
    end_local = datetime.combine(today, dt_time.max, tzinfo=TZ)
    return (
        start_local.astimezone(timezone.utc),
        end_local.astimezone(timezone.utc),
        start_day.isoformat(),
        today.isoformat(),
    )


def gdelt_search(query: str, start_utc: datetime, end_utc: datetime, category: str) -> list[dict[str, Any]]:
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(MAX_PER_QUERY),
        "sort": "DateDesc",
        "startdatetime": start_utc.strftime("%Y%m%d%H%M%S"),
        "enddatetime": end_utc.strftime("%Y%m%d%H%M%S"),
    }
    for attempt in range(3):
        try:
            resp = requests.get(GDELT_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=35)
            resp.raise_for_status()
            articles = resp.json().get("articles", [])
            if not isinstance(articles, list):
                return []
            cleaned = []
            for article in articles:
                if not isinstance(article, dict) or not article.get("url"):
                    continue
                cleaned.append({
                    "title": str(article.get("title") or "").strip(),
                    "url": str(article.get("url") or "").strip(),
                    "domain": str(article.get("domain") or urlparse(str(article.get("url"))).netloc).strip(),
                    "language": str(article.get("language") or "Unknown").strip(),
                    "source_country": str(article.get("sourcecountry") or "Unknown").strip(),
                    "gdelt_seen_date": str(article.get("seendate") or "").strip(),
                    "discovery_category": category,
                })
            return cleaned
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                logging.warning("GDELT sorgusu basarisiz (%s): %s", category, exc)
                return []
            time.sleep(2**attempt)
    return []


def walk_jsonld(obj: Any, found: dict[str, Any]) -> None:
    if isinstance(obj, dict):
        for key in ("datePublished", "dateCreated", "headline", "name", "articleBody"):
            if key in obj and key not in found and isinstance(obj[key], (str, int, float)):
                found[key] = obj[key]
        publisher = obj.get("publisher")
        if isinstance(publisher, dict) and "publisher" not in found:
            found["publisher"] = publisher.get("name")
        for value in obj.values():
            walk_jsonld(value, found)
    elif isinstance(obj, list):
        for item in obj:
            walk_jsonld(item, found)


def inspect_page(candidate: dict[str, Any]) -> dict[str, Any]:
    result = dict(candidate)
    result.update({
        "page_access_status": "Not accessible",
        "page_title": "",
        "publisher_from_page": "",
        "publication_date_from_page": "",
        "page_text_excerpt": "",
        "automated_check_note": "Not independently verified; Antigravity must inspect the original page.",
    })
    try:
        resp = requests.get(candidate["url"], headers={"User-Agent": USER_AGENT}, timeout=18, allow_redirects=True)
        result["final_url"] = resp.url
        result["http_status"] = resp.status_code
        if resp.status_code >= 400:
            result["page_access_status"] = f"HTTP {resp.status_code}; content not inspected"
            return result
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype.lower() and not resp.text.lstrip().startswith("<"):
            result["page_access_status"] = f"Accessible, non-HTML content ({ctype or 'unknown type'})"
            return result
        soup = BeautifulSoup(resp.text, "html.parser")
        title_tag = soup.find("meta", attrs={"property": "og:title"}) or soup.find("meta", attrs={"name": "twitter:title"})
        page_title = (title_tag.get("content", "") if title_tag else "") or (soup.title.get_text(" ", strip=True) if soup.title else "")
        site_tag = soup.find("meta", attrs={"property": "og:site_name"})
        publisher = site_tag.get("content", "") if site_tag else ""
        pub_date = ""
        for attrs in (
            {"property": "article:published_time"},
            {"name": "datePublished"},
            {"name": "pubdate"},
            {"name": "date"},
            {"property": "og:updated_time"},
        ):
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                pub_date = tag.get("content", "").strip()
                break
        jsonld_found: dict[str, Any] = {}
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                walk_jsonld(json.loads(script.string or script.get_text()), jsonld_found)
            except (json.JSONDecodeError, TypeError):
                continue
        pub_date = pub_date or str(jsonld_found.get("datePublished") or jsonld_found.get("dateCreated") or "")
        publisher = publisher or str(jsonld_found.get("publisher") or "")
        page_title = page_title or str(jsonld_found.get("headline") or "")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
            tag.decompose()
        article = soup.find("article") or soup.find("main") or soup.body or soup
        text = normalize_space(article.get_text(" ", strip=True))
        result.update({
            "page_access_status": "HTTP 200; HTML fetched (automated inspection only)",
            "page_title": page_title[:500],
            "publisher_from_page": publisher[:250],
            "publication_date_from_page": pub_date[:120],
            "page_text_excerpt": text[:PAGE_TEXT_LIMIT],
        })
        return result
    except requests.RequestException as exc:
        result["page_access_status"] = f"Request failed: {type(exc).__name__}"
        return result
    except Exception as exc:
        result["page_access_status"] = f"Page parsing issue: {type(exc).__name__}"
        return result


def select_stratified_sample(candidates: list[dict[str, Any]], sample_size: int) -> list[dict[str, Any]]:
    if sample_size <= 0 or sample_size >= len(candidates):
        return list(candidates)

    selected: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    def add_item(item: dict[str, Any]) -> None:
        key = item.get("url", "")
        if key and key not in seen_urls and len(selected) < sample_size:
            seen_urls.add(key)
            selected.append(item)

    def is_http_200(item: dict[str, Any]) -> bool:
        return "HTTP 200" in str(item.get("page_access_status", ""))

    for item in candidates:
        if item.get("discovery_category") == "De-Russification" and is_http_200(item):
            add_item(item)

    for item in candidates:
        if not is_http_200(item) and item.get("discovery_category") == "De-Russification":
            add_item(item)
            break
    else:
        for item in candidates:
            if not is_http_200(item):
                add_item(item)
                break

    buckets = {
        cat: [c for c in candidates if c.get("discovery_category") == cat and is_http_200(c) and c.get("url") not in seen_urls]
        for cat in ("Regional actors", "Lithuania", "Ukraine")
    }
    while len(selected) < sample_size and any(buckets.values()):
        for cat in ("Regional actors", "Lithuania", "Ukraine"):
            if buckets[cat] and len(selected) < sample_size:
                add_item(buckets[cat].pop(0))

    for item in candidates:
        if len(selected) >= sample_size:
            break
        add_item(item)

    return selected


def assign_candidate_ids(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign deterministic, unique C001..CNNN IDs strictly from original input order."""
    existing = [str(c.get("candidate_id") or "").strip() for c in candidates]
    all_valid_unique = (
        len(candidates) > 0
        and all(bool(cid) for cid in existing)
        and len(set(existing)) == len(candidates)
    )
    assigned: list[dict[str, Any]] = []
    for idx, cand in enumerate(candidates, start=1):
        copy_item = dict(cand)
        if not all_valid_unique:
            copy_item["candidate_id"] = f"C{idx:03d}"
        assigned.append(copy_item)
    return assigned


def chunk_candidates(candidates: list[dict[str, Any]], batch_size: int) -> list[list[dict[str, Any]]]:
    size = max(1, int(batch_size))
    return [candidates[i : i + size] for i in range(0, len(candidates), size)]


def build_candidate_payload(candidates: list[dict[str, Any]], include_urls: bool = False) -> list[dict[str, Any]]:
    """Build model-facing candidate JSON, omitting URLs in candidate-ID mode to prevent URL mutation."""
    payload = []
    for item in candidates:
        entry: dict[str, Any] = {
            "candidate_id": item.get("candidate_id", ""),
            "title_from_gdelt": item.get("title", ""),
        }
        if include_urls:
            entry["original_url"] = item.get("url", "")
            entry["final_url_after_redirect"] = item.get("final_url", "")
        entry.update({
            "publisher_domain": item.get("domain", ""),
            "publisher_from_page": item.get("publisher_from_page", ""),
            "language": item.get("language", ""),
            "source_country": item.get("source_country", ""),
            "gdelt_seen_date_not_publication_date": item.get("gdelt_seen_date", ""),
            "discovery_category": item.get("discovery_category", ""),
            "page_access_status": item.get("page_access_status", ""),
            "page_title": item.get("page_title", ""),
            "publication_date_from_page": item.get("publication_date_from_page", ""),
            "page_text_excerpt": item.get("page_text_excerpt", ""),
        })
        payload.append(entry)
    return payload


def build_model_prompt(
    template: str,
    candidates: list[dict[str, Any]],
    start_date: str,
    end_date: str,
    run_date: str,
    is_sample: bool,
) -> str:
    source_blob = json.dumps(build_candidate_payload(candidates, include_urls=True), ensure_ascii=False, indent=2)
    sample_guard = ""
    if is_sample:
        sample_guard = (
            f"\n\n## ÖRNEKLEM TEST NOTU\n"
            f"Bu çalışma {len(candidates)} adaylık örneklemdir. Yalnızca verilen adayları analiz et, yeni URL veya kaynak ekleme.\n"
        )
    return (
        template.replace("{START_DATE}", start_date)
        .replace("{END_DATE}", end_date)
        .replace("{RUN_DATE}", run_date)
        .replace("{CANDIDATE_COUNT}", str(len(candidates)))
        + sample_guard
        + "\n\n## GDELT tarafından bulunan aday kayıtlar\n"
        + "Aşağıdaki JSON tek kaynak keşif havuzudur. JSON'da olmayan URL veya kaynak ekleme.\n\n"
        + source_blob
    )


def build_structured_messages(
    template: str,
    candidates: list[dict[str, Any]],
    start_date: str,
    end_date: str,
    run_date: str,
    batch_index: int = 1,
    total_batches: int = 1,
) -> list[dict[str, str]]:
    source_blob = json.dumps(build_candidate_payload(candidates, include_urls=False), ensure_ascii=False, indent=2)
    system_prompt = (
        "Sen Baltık çalışmaları, Rusya/Ukrayna güvenliği ve medya analizi alanında çalışan titiz bir "
        "OSINT kaynak sınıflandırma asistanısın. Yanıtın YALNIZCA geçerli, eksiksiz tek bir JSON nesnesi olmalıdır.\n\n"
        "KESİN KURALLAR (ASLA İHLAL ETME):\n"
        "1. ADAY KİMLİĞİ (`candidate_id`) VE URL KURALI:\n"
        "   - Her kaydı YALNIZCA girdide verilen birebir `candidate_id` (ör. `C001`) ile tanımla.\n"
        "   - `records` veya `top_5_valuable_candidate_ids` içine URL KOPYALAMA, YAZMA veya UYDURMA; orijinal URL, yönlendirme URL'si, "
        "tarih, dil ve yayıncı alanları Python tarafından girdi JSON'undan otomatik geri yüklenir.\n"
        "2. İKİ AŞAMALI DOĞRULAMA VE METİN TUTARLILIĞI:\n"
        "   - `Verified` etiketini ASLA kullanma.\n"
        "   - `page_access_verification_status`: Girdide `page_access_status` içinde 'HTTP 200' VE dolu bir `page_text_excerpt` "
        "varsa `Partially verified`; aksi hâlde `Not verified` yaz.\n"
        "   - `factual_claim_verification_status`: Her zaman `Pending Antigravity primary inspection` yaz.\n"
        "   - KRİTİK TUTARLILIK: Eğer girdide `page_text_excerpt` DOLU ise, mutlaka çıkarılan sayfa metnini özetle ve ASLA "
        "'Sayfa içeriğine otomatik erişilemedi' veya 'yalnızca GDELT başlığı mevcuttur' yazma! Yalnızca `page_text_excerpt` BOŞ "
        "olduğunda sayfa içeriğine erişilemediğini açıkça belirt ve başlıktan gövde içeriği tahmin etme.\n"
        "3. BAŞLIK VE YAYIN TARİHİ KORUMASI: İmla/noktalama temizliği önereceksen bunu ayrı `normalized_title` alanına yaz "
        "(gerekmiyorsa `\"\"` bırak). `original_title`, `publication_date`, `publication_language` ve `publisher_country` "
        "değerlerini girdiyle tutarlı tut; `gdelt_seen_date_not_publication_date` değerini yayın tarihi olarak kullanma.\n"
        "4. KAYNAK TÜRÜ AYRIMI: `source_type` alanı şu 5 değerden biri olmalıdır: "
        "`News Article`, `Opinion/Commentary`, `Government/Official Document`, `Academic Publication`, `Think Tank Report`. "
        "Haber/gazete/blog sitelerini asla `Government/Official Document` veya `Academic Publication` olarak işaretleme; "
        "`is_primary_official_document` bunlarda `false` boolean olmalıdır.\n"
        "5. COĞRAFYA AYRIMI: `geographic_relevance` alanında yayıncının ülkesini değil, metinde GERÇEKTEN ele alınan ülke/bölgeleri yaz.\n"
        "6. TEZ İLİŞKİSİ SINIFLAMASI (4 DÜZEY — ABARTMA YASAK):\n"
        "   Her Baltık/Litvanya/Ukrayna haberini OTOMATİK OLARAK `Direct` işaretleme! `thesis_relevance_tier` yalnız şu 4 değerden biri olabilir:\n"
        "   - `Direct`: YALNIZCA Estonya veya Letonya'daki de-Rusifikasyon, Rusça konuşan azınlıklar, dil/eğitim/vatandaşlık/medya/anıt/kilise "
        "politikaları veya dış krize iç politika tepkisi hakkında AÇIK kanıt varsa.\n"
        "   - `Indirect`: Litvanya, Polonya veya bölgesel karşılaştırma açısından faydalı bağlam ya da azınlık/medya karşılaştırma verisi sunuyorsa.\n"
        "   - `Background`: Doğrudan analitik bağ olmaksızın genel jeopolitik, NATO, Ukrayna savaşı veya tarihsel arka plan sunuyorsa.\n"
        "   - `Irrelevant`: Araştırma soruları ile anlamlı bağlantısı yoksa (yine de kaydı DÜŞÜRME, `Irrelevant` olarak raporla).\n"
        "7. TAM KAPSAM VE SIFIR UYDURMA: Partideki TÜM `candidate_id` değerleri için (`records` dizisinde her `candidate_id` için tam 1 kayıt) çıktı üret."
    )

    user_prompt = (
        f"Araştırma penceresi: {start_date} – {end_date} | Çalıştırma tarihi: {run_date} | "
        f"Parti: {batch_index}/{total_batches} | Partideki aday sayısı: {len(candidates)}\n"
        f"Tez odağı: Dış Krizin İç Siyasete Etkisi: Letonya ve Estonya'da De-Rusifikasyon.\n"
        "Aşağıdaki şemaya birebir uyan tek bir JSON nesnesi döndür (URL ekleme; her kaydı `candidate_id` ile referansla):\n"
        "{\n"
        '  "executive_summary": "Bu parti için kısa değerlendirme (Türkçe)",\n'
        '  "official_document_gap_note": "Partide resmî birincil belge bulunup bulunmadığına dair açık not",\n'
        '  "records": [\n'
        "    {\n"
        '      "candidate_id": "girdideki birebir candidate_id (ör. C001)",\n'
        '      "normalized_title": "yalnızca temizlenmiş başlık önerisi varsa, yoksa boş string",\n'
        '      "publisher": "yayıncı kuruluş",\n'
        '      "event_date": "olay tarihi açıkça biliniyorsa veya Belirlenemedi",\n'
        '      "source_type": "News Article | Opinion/Commentary | Government/Official Document | Academic Publication | Think Tank Report",\n'
        '      "is_primary_official_document": false,\n'
        '      "geographic_relevance": ["gerçekte konu edilen ülke/bölgeler"],\n'
        '      "page_access_verification_status": "Partially verified | Not verified",\n'
        '      "factual_claim_verification_status": "Pending Antigravity primary inspection",\n'
        '      "content_summary": "page_text_excerpt doluysa metne dayalı özet (asla erişilemedi yazma); boşsa sayfa metni yok notu",\n'
        '      "thesis_relevance_tier": "Direct | Indirect | Background | Irrelevant",\n'
        '      "thesis_relevance": "seçilen düzeyin kanıta dayalı gerekçesi (abartısız ve dürüst)",\n'
        '      "reliability_notes": "sahiplik/siyasi konum/erişim durumu ve Antigravity tarafından neyin kontrol edilmesi gerektiği"\n'
        "    }\n"
        "  ],\n"
        '  "top_5_valuable_candidate_ids": ["bu partide tez açısından en değerli en fazla 5 girdi candidate_id"],\n'
        '  "research_questions": ["bu kayıtlara dayalı en fazla 3 araştırma sorusu"],\n'
        '  "verification_priorities": ["Antigravity tarafından doğrulanması gereken ilk 5 husus"]\n'
        "}\n\n"
        "## Aday JSON Girdisi\n"
        f"{source_blob}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def audit_urls(text: str, candidates: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    allowed: set[str] = set()
    for item in candidates:
        for key in ("url", "final_url"):
            val = str(item.get(key) or "").strip()
            if val:
                allowed.add(val)
                allowed.add(val.rstrip("/"))
    found_raw = re.findall(r"https?://[^\s\)\]>\"'`\\]+", text)
    found_clean = [u.rstrip(".,;:!?") for u in found_raw]
    unmatched = [u for u in dict.fromkeys(found_clean) if u not in allowed and u.rstrip("/") not in allowed]
    return list(dict.fromkeys(found_clean)), unmatched


def expected_page_access_verification_status(cand: dict[str, Any]) -> str:
    """Only HTTP 200 with non-empty inspected excerpt earns Partially verified; never Verified."""
    access = str(cand.get("page_access_status") or "")
    excerpt = str(cand.get("page_text_excerpt") or "").strip()
    if "HTTP 200" in access and len(excerpt) > 0:
        return "Partially verified"
    return "Not verified"


def check_geographic_grounding(geo_list: Any, cand: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(geo_list, list) or not geo_list:
        return ["missing_or_empty_geographic_relevance"]
    haystack = " ".join([
        str(cand.get("title") or ""),
        str(cand.get("page_title") or ""),
        str(cand.get("page_text_excerpt") or ""),
        str(cand.get("source_country") or ""),
    ]).lower()

    country_evidence_patterns = {
        "latvia": r"(latv|letony|riika|riga\b|lettland|łotw|латв)",
        "estonia": r"(eston|eesti|tallinn|estland|narva|эстон)",
        "lithuania": r"(lithuan|lietuv|litvany|litau|lituani|wiln|vilni|litw|литв|kaliningrad)",
    }
    alias_map = {
        "letonya": "latvia",
        "latvia": "latvia",
        "estonia": "estonia",
        "estonya": "estonia",
        "litvanya": "lithuania",
        "lithuania": "lithuania",
    }
    for entry in geo_list:
        low = str(entry).strip().lower()
        for alias, canonical in alias_map.items():
            if alias in low and not re.search(country_evidence_patterns[canonical], haystack):
                errors.append(f"ungrounded_country:{entry}")
    return errors


def infer_thesis_relevance_tier(rec: dict[str, Any]) -> tuple[str, bool]:
    """Extract canonical relevance tier from explicit field or legacy Turkish text."""
    explicit = str(rec.get("thesis_relevance_tier") or "").strip()
    if explicit:
        for allowed in ALLOWED_RELEVANCE_TIERS:
            if explicit.lower() == allowed.lower():
                return allowed, True
        return explicit, False

    text = str(rec.get("thesis_relevance") or "").strip().lower()
    if text.startswith("doğrudan") or "doğrudan ilişki" in text[:45] or "direct relevance" in text[:45]:
        return "Direct", True
    if text.startswith("dolaylı") or "dolaylı ilişki" in text[:45] or "indirect relevance" in text[:45]:
        return "Indirect", True
    if text.startswith("arka plan") or "arka plan" in text[:45] or "bağlamsal" in text[:45] or "background" in text[:45]:
        return "Background", True
    if text.startswith("ilgisiz") or "ilgisiz" in text[:45] or "doğrudan ilgili değildir" in text[:65] or "irrelevant" in text[:45]:
        return "Irrelevant", True
    return "Background", False


def evaluate_thesis_relevance_guardrail(cand: dict[str, Any], raw_tier: str) -> tuple[str, list[str], str]:
    """Prevent Baltic/Lithuania/generic news articles from auto-qualifying as Direct."""
    warnings: list[str] = []
    title_text = " ".join([
        str(cand.get("title") or ""),
        str(cand.get("page_title") or ""),
    ])
    full_text = f"{title_text} {cand.get('page_text_excerpt') or ''}"

    has_ee_lv = bool(ESTONIA_LATVIA_EVIDENCE_RE.search(full_text))
    has_policy = bool(DERUSSIFICATION_POLICY_EVIDENCE_RE.search(full_text))
    has_comp = bool(COMPARATIVE_CONTEXT_EVIDENCE_RE.search(full_text))
    has_excerpt = bool(str(cand.get("page_text_excerpt") or "").strip())

    if raw_tier != "Direct":
        if raw_tier == "Indirect" and not has_comp and not (has_ee_lv and has_policy):
            warnings.append("ungrounded_indirect_without_comparative_evidence")
        return raw_tier, warnings, ""

    if has_ee_lv and has_policy:
        if not has_excerpt:
            note = "Başlık düzeyinde doğrudan Estonya/Letonya de-Rusifikasyon sinyali var; ancak sayfa metni doğrulanmadığı için kesinlik içerik incelemesine bağlıdır."
            warnings.append("direct_headline_only_needs_inspection")
            return "Direct", warnings, note
        return "Direct", warnings, ""

    downgraded_tier = "Indirect" if (has_ee_lv or has_comp) else "Background"
    warnings.append(f"baltic_overclaim_prevented:Direct->{downgraded_tier}")
    reason_bits = []
    if not has_ee_lv:
        reason_bits.append("metinde açık Estonya/Letonya odağı bulunamadı")
    if not has_policy:
        reason_bits.append("de-Rusifikasyon/azınlık/dil/eğitim/vatandaşlık/medya/iç politika kanıtı bulunamadı")
    note = (
        f"Deterministik tez koruma kuralı `Direct` etiketini `{downgraded_tier}` düzeyine düşürdü "
        f"({'; '.join(reason_bits)})."
    )
    return downgraded_tier, warnings, note


def is_official_source_domain(domain: str) -> bool:
    return bool(OFFICIAL_INSTITUTION_DOMAIN_RE.search((domain or "").strip()))


def build_url_lookup(candidates: list[dict[str, Any]]) -> dict[str, int]:
    """Map exact/final/trailing-slash URLs to their candidate index in the input slice."""
    lookup: dict[str, int] = {}
    for idx, cand in enumerate(candidates):
        for key in ("url", "final_url"):
            val = str(cand.get(key) or "").strip()
            if val:
                lookup.setdefault(val, idx)
                lookup.setdefault(val.rstrip("/"), idx)
    return lookup


def build_input_snapshot(cand: dict[str, Any]) -> dict[str, Any]:
    """Preserve complete verbatim input provenance fields without altering anything."""
    pub_date = str(cand.get("publication_date_from_page") or "").strip() or "Belirlenemedi"
    return {
        "candidate_id": str(cand.get("candidate_id") or "").strip(),
        "original_url": str(cand.get("url") or "").strip(),
        "final_url": str(cand.get("final_url") or "").strip(),
        "original_title": str(cand.get("title") or "").strip() or str(cand.get("page_title") or "").strip(),
        "page_title_from_input": str(cand.get("page_title") or "").strip(),
        "publisher_domain": str(cand.get("domain") or "").strip(),
        "publisher_from_page": str(cand.get("publisher_from_page") or "").strip(),
        "publisher_country": str(cand.get("source_country") or "Unknown").strip(),
        "publication_language": str(cand.get("language") or "Unknown").strip(),
        "publication_date": pub_date,
        "gdelt_seen_date": str(cand.get("gdelt_seen_date") or "").strip(),
        "discovery_category": str(cand.get("discovery_category") or "").strip(),
        "page_access_status": str(cand.get("page_access_status") or "").strip(),
        "http_status": cand.get("http_status"),
        "page_access_verification_status": expected_page_access_verification_status(cand),
        "factual_claim_verification_status": FACTUAL_CLAIM_VERIFICATION_DEFAULT,
    }


def make_non_valid_entry(
    cand: dict[str, Any],
    batch_index: int,
    reasons: list[str],
    raw_model_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = build_input_snapshot(cand)
    entry.update({
        "batch_index": batch_index,
        "reasons": list(reasons),
    })
    if raw_model_record is not None:
        entry["raw_model_record"] = raw_model_record
    return entry


def check_access_narrative_consistency(
    cand: dict[str, Any],
    rec: dict[str, Any],
    rec_label: str,
    cid_or_url: str,
    audit: dict[str, Any],
) -> list[str]:
    """Detect contradictions between recorded input page access and model summary/narrative."""
    flags: list[str] = []
    has_extracted_text = expected_page_access_verification_status(cand) == "Partially verified"
    summary_text = str(rec.get("content_summary") or "")
    narrative_text = f"{rec.get('thesis_relevance') or ''} {rec.get('reliability_notes') or ''}"

    if has_extracted_text:
        if INACCESSIBLE_CLAIM_RE.search(summary_text):
            flags.append("summary_contradicts_extracted_page_text")
            audit["contradictory_summary_flags"].append(
                f"{rec_label}:summary_contradicts_extracted_page_text({cid_or_url})"
            )
        elif INACCESSIBLE_CLAIM_RE.search(narrative_text):
            flags.append("narrative_contradicts_page_access_evidence")
            audit["contradictory_summary_flags"].append(
                f"{rec_label}:narrative_contradicts_page_access_evidence({cid_or_url})"
            )
        return flags

    summary_low = summary_text.lower()
    if not INACCESSIBLE_CLAIM_RE.search(summary_text) and not any(
        kw in summary_low for kw in ("erişil", "alınamad", "mevcut değil", "yok", "incelenemed", "not")
    ):
        flags.append("missing_inaccessible_content_disclaimer")
        audit["headline_only_inference_warnings"].append(
            f"{rec_label}:missing_inaccessible_content_disclaimer({cid_or_url})"
        )
    return flags


def lock_and_validate_record(
    rec: dict[str, Any],
    cand: dict[str, Any],
    rec_label: str,
    batch_index: int,
    audit: dict[str, Any],
    allow_legacy_url_lookup: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate substantive fields, lock exact input provenance, and check access consistency."""
    rejection_reasons: list[str] = []
    review_flags: list[str] = []
    snapshot = build_input_snapshot(cand)
    cid_or_url = snapshot["candidate_id"] or snapshot["original_url"]

    req_fields = (
        REQUIRED_SUBSTANTIVE_FIELDS_LEGACY
        if allow_legacy_url_lookup and "candidate_id" not in rec
        else REQUIRED_SUBSTANTIVE_FIELDS
    )
    for rf in req_fields:
        val = rec.get(rf)
        if val is None or (isinstance(val, str) and not val.strip()):
            audit["missing_record_fields"].append(f"{rec_label}:{rf}")
            rejection_reasons.append(f"missing_field:{rf}")

    # Page-access status is ALWAYS derived from input evidence; contradictions trigger review/rejection.
    raw_ver = str(
        rec.get("page_access_verification_status") or rec.get("verification_status") or ""
    ).strip()
    exp_ver = snapshot["page_access_verification_status"]
    if not raw_ver:
        audit["missing_record_fields"].append(f"{rec_label}:page_access_verification_status")
        rejection_reasons.append("missing_field:page_access_verification_status")
    elif raw_ver == "Verified" or raw_ver not in ALLOWED_PAGE_ACCESS_STATUSES:
        audit["verification_errors"].append(f"{rec_label}:invalid_page_access_status:{raw_ver}")
        rejection_reasons.append(f"verification_violation:{raw_ver}")
    elif exp_ver == "Not verified" and raw_ver == "Partially verified":
        audit["verification_errors"].append(
            f"{rec_label}:unauthorized_upgrade(got={raw_ver},expected={exp_ver})"
        )
        rejection_reasons.append(f"verification_access_mismatch:{raw_ver}!=expected({exp_ver})")
    elif exp_ver == "Partially verified" and raw_ver == "Not verified":
        flag = f"model_status_underclaimed_vs_input_evidence(model={raw_ver},input={exp_ver})"
        review_flags.append(flag)
        audit["contradictory_summary_flags"].append(f"{rec_label}:{flag}({cid_or_url})")

    review_flags.extend(check_access_narrative_consistency(cand, rec, rec_label, cid_or_url, audit))

    # Source type and primary official document check
    got_type = str(rec.get("source_type") or "").strip()
    raw_primary = rec.get("is_primary_official_document")
    is_primary = (raw_primary is True) or (isinstance(raw_primary, str) and raw_primary.strip().lower() == "true")
    domain = snapshot["publisher_domain"].lower()
    if got_type not in ALLOWED_SOURCE_TYPES:
        audit["source_type_errors"].append(f"{rec_label}:invalid_source_type:{got_type}")
        rejection_reasons.append(f"invalid_source_type:{got_type}")
    elif (domain in KNOWN_NON_OFFICIAL_DOMAINS or not is_official_source_domain(domain)) and got_type in (
        "Government/Official Document",
        "Academic Publication",
    ):
        if got_type == "Government/Official Document" or domain in KNOWN_NON_OFFICIAL_DOMAINS:
            audit["source_type_errors"].append(
                f"{rec_label}:non_official_domain_misclassified_as_{got_type}({domain})"
            )
            rejection_reasons.append(f"source_type_violation:{got_type}")
    if (domain in KNOWN_NON_OFFICIAL_DOMAINS or not is_official_source_domain(domain)) and is_primary:
        audit["source_type_errors"].append(
            f"{rec_label}:non_official_domain_marked_is_primary_official_document({domain})"
        )
        rejection_reasons.append("primary_official_document_violation")

    # Language / country / title / date provenance checks & deterministic restoration from input
    corrections: list[str] = []
    got_lang = str(rec.get("publication_language") or "").strip()
    exp_lang = snapshot["publication_language"]
    if got_lang and exp_lang and got_lang.lower() != exp_lang.lower():
        audit["language_errors"].append(f"{rec_label}:lang_mismatch(got={got_lang},expected={exp_lang})")
        corrections.append(f"publication_language_locked:{got_lang}->{exp_lang}")

    got_country = str(rec.get("publisher_country") or "").strip()
    exp_country = snapshot["publisher_country"]
    if got_country and exp_country and got_country.lower() != exp_country.lower():
        audit["geographic_errors"].append(
            f"{rec_label}:publisher_country_mismatch(got={got_country},expected={exp_country})"
        )
        corrections.append(f"publisher_country_locked:{got_country}->{exp_country}")

    geo_errs = check_geographic_grounding(rec.get("geographic_relevance"), cand)
    for ge in geo_errs:
        audit["geographic_errors"].append(f"{rec_label}:{ge}")

    model_orig_title = str(rec.get("original_title") or "").strip()
    model_norm_title = str(rec.get("normalized_title") or "").strip()
    if model_orig_title and normalize_space(model_orig_title) != normalize_space(snapshot["original_title"]):
        audit["provenance_corrections"].append(
            f"{rec_label}:title_locked_to_input(model_norm={model_orig_title[:60]})"
        )
        corrections.append("original_title_restored_from_input")
    normalized_title = model_norm_title
    if not normalized_title and model_orig_title and model_orig_title != snapshot["original_title"]:
        normalized_title = model_orig_title

    exp_date = str(cand.get("publication_date_from_page") or "").strip()
    got_date = str(rec.get("publication_date") or "").strip()
    gdelt_seen = snapshot["gdelt_seen_date"]
    if got_date:
        if exp_date:
            if got_date != exp_date and not exp_date.startswith(got_date[:10]):
                audit["provenance_corrections"].append(
                    f"{rec_label}:date_locked(got={got_date},expected={exp_date})"
                )
                corrections.append("publication_date_restored_from_input")
        else:
            if got_date != "Belirlenemedi" or (gdelt_seen and gdelt_seen[:8] in got_date.replace("-", "")):
                audit["provenance_corrections"].append(
                    f"{rec_label}:date_locked_Belirlenemedi(got={got_date})"
                )
                corrections.append("publication_date_locked_to_Belirlenemedi")

    # Thesis relevance tier extraction and deterministic guardrail
    inferred_tier, tier_ok = infer_thesis_relevance_tier(rec)
    if not tier_ok and str(rec.get("thesis_relevance_tier") or "").strip():
        audit["relevance_overclaim_warnings"].append(f"{rec_label}:invalid_relevance_tier:{inferred_tier}")
        inferred_tier = "Background"
    enforced_tier, tier_warnings, guardrail_note = evaluate_thesis_relevance_guardrail(cand, inferred_tier)
    for tw in tier_warnings:
        audit["relevance_overclaim_warnings"].append(f"{rec_label}:{tw}({cid_or_url})")

    if rejection_reasons:
        return None, rejection_reasons

    dedup_review_flags = list(dict.fromkeys(review_flags))
    if dedup_review_flags:
        audit["review_flagged_records"].append(f"{rec_label}:{cid_or_url}:{','.join(dedup_review_flags)}")

    locked_record = {
        "batch_index": batch_index,
        "candidate_id": snapshot["candidate_id"],
        "original_url": snapshot["original_url"],
        "final_url": snapshot["final_url"],
        "original_title": snapshot["original_title"],
        "normalized_title": normalized_title,
        "page_title_from_input": snapshot["page_title_from_input"],
        "publisher": snapshot["publisher_from_page"] or str(rec.get("publisher") or "").strip() or snapshot["publisher_domain"],
        "publisher_domain": snapshot["publisher_domain"],
        "publisher_from_page": snapshot["publisher_from_page"],
        "publisher_country": snapshot["publisher_country"],
        "publication_language": snapshot["publication_language"],
        "publication_date": snapshot["publication_date"],
        "gdelt_seen_date": snapshot["gdelt_seen_date"],
        "discovery_category": snapshot["discovery_category"],
        "event_date": str(rec.get("event_date") or "Belirlenemedi").strip(),
        "source_type": got_type,
        "is_primary_official_document": is_primary,
        "geographic_relevance": list(rec.get("geographic_relevance") or []),
        "page_access_status": snapshot["page_access_status"],
        "page_access_verification_status": snapshot["page_access_verification_status"],
        "factual_claim_verification_status": FACTUAL_CLAIM_VERIFICATION_DEFAULT,
        "verification_status": snapshot["page_access_verification_status"],
        "needs_review": bool(dedup_review_flags),
        "review_flags": dedup_review_flags,
        "content_summary": str(rec.get("content_summary") or "").strip(),
        "thesis_relevance_tier_model": inferred_tier,
        "thesis_relevance_tier": enforced_tier,
        "thesis_relevance_tier_tr": RELEVANCE_TIER_LABELS_TR.get(enforced_tier, enforced_tier),
        "thesis_relevance": str(rec.get("thesis_relevance") or "").strip(),
        "relevance_guardrail_note": guardrail_note,
        "reliability_notes": str(rec.get("reliability_notes") or "").strip(),
        "provenance_corrections": corrections,
    }
    return locked_record, []


def validate_structured_batch(
    raw_content: str,
    finish_reason: str,
    batch_candidates: list[dict[str, Any]],
    batch_index: int = 1,
    allow_legacy_url_lookup: bool = False,
) -> dict[str, Any]:
    """Validate one structured JSON batch using deterministic candidate_id lookup (never guessing)."""
    audit: dict[str, Any] = {
        "batch_index": batch_index,
        "malformed_json": False,
        "finish_reason": finish_reason,
        "truncated": finish_reason != "stop",
        "expected_candidate_count": len(batch_candidates),
        "returned_record_count": 0,
        "missing_top_fields": [],
        "missing_record_fields": [],
        "candidate_id_errors": [],
        "missing_candidate_ids": [],
        "duplicate_candidate_ids": [],
        "url_provenance_errors": [],
        "missing_input_urls": [],
        "duplicate_output_urls": [],
        "verification_errors": [],
        "contradictory_summary_flags": [],
        "review_flagged_records": [],
        "headline_only_inference_warnings": [],
        "language_errors": [],
        "geographic_errors": [],
        "source_type_errors": [],
        "provenance_corrections": [],
        "relevance_overclaim_warnings": [],
        "total_error_count": 0,
        "validation_passed": False,
    }

    try:
        parsed = json.loads(raw_content)
    except ValueError as exc:
        audit["malformed_json"] = True
        audit["json_error"] = str(exc)
        audit["total_error_count"] = 1
        rejected = [
            make_non_valid_entry(cand, batch_index, [f"malformed_batch_json:{exc}"])
            for cand in batch_candidates
        ]
        return {
            "parsed": None,
            "audit": audit,
            "validated_records": [],
            "rejected_or_incomplete_records": rejected,
            "fabricated_outputs": [],
        }

    if not isinstance(parsed, dict):
        audit["malformed_json"] = True
        audit["json_error"] = "Top-level JSON value is not an object"
        audit["total_error_count"] = 1
        rejected = [
            make_non_valid_entry(cand, batch_index, ["top_level_json_not_object"])
            for cand in batch_candidates
        ]
        return {
            "parsed": None,
            "audit": audit,
            "validated_records": [],
            "rejected_or_incomplete_records": rejected,
            "fabricated_outputs": [],
        }

    for field_name in REQUIRED_TOP_FIELDS:
        if field_name not in parsed or parsed[field_name] in (None, ""):
            audit["missing_top_fields"].append(field_name)
    if (
        ("top_5_valuable_candidate_ids" not in parsed or parsed.get("top_5_valuable_candidate_ids") is None)
        and ("top_5_valuable_urls" not in parsed or parsed.get("top_5_valuable_urls") is None)
    ):
        audit["missing_top_fields"].append("top_5_valuable_candidate_ids")

    found_urls, unmatched_urls = audit_urls(raw_content, batch_candidates)
    audit["urls_emitted_count"] = len(found_urls)
    if unmatched_urls:
        audit["url_provenance_errors"].extend([f"unmatched_url:{u}" for u in unmatched_urls])

    cand_by_id = {
        str(c.get("candidate_id") or "").strip(): idx
        for idx, c in enumerate(batch_candidates)
        if str(c.get("candidate_id") or "").strip()
    }
    url_lookup = build_url_lookup(batch_candidates)
    records = parsed.get("records")
    if not isinstance(records, list):
        audit["missing_top_fields"].append("records(not_a_list)")
        records = []

    audit["returned_record_count"] = len(records)
    handled_indices: set[int] = set()
    validated_records: list[dict[str, Any]] = []
    rejected_records: list[dict[str, Any]] = []
    fabricated_outputs: list[dict[str, Any]] = []

    for r_pos, rec in enumerate(records, start=1):
        rec_label = f"b{batch_index}_r{r_pos}"
        if not isinstance(rec, dict):
            audit["missing_record_fields"].append(f"{rec_label}:not_an_object")
            fabricated_outputs.append({"label": rec_label, "reason": "record_not_an_object", "raw": rec})
            continue

        raw_cid = rec.get("candidate_id")
        raw_url = str(rec.get("original_url") or "").strip()
        cand_idx: int | None = None

        if allow_legacy_url_lookup and raw_cid is None:
            cand_idx = url_lookup.get(raw_url)
            if cand_idx is None:
                cand_idx = url_lookup.get(raw_url.rstrip("/"))
            if cand_idx is None:
                audit["url_provenance_errors"].append(f"{rec_label}:unknown_original_url:{raw_url}")
                fabricated_outputs.append({"label": rec_label, "reason": "unknown_original_url", "original_url": raw_url})
                continue
        else:
            if not isinstance(raw_cid, str) or not raw_cid.strip():
                audit["candidate_id_errors"].append(f"{rec_label}:missing_or_invalid_candidate_id:{raw_cid}")
                fabricated_outputs.append({"label": rec_label, "reason": "missing_or_invalid_candidate_id", "candidate_id": raw_cid})
                continue
            cid = raw_cid.strip()
            if cid not in cand_by_id:
                audit["candidate_id_errors"].append(f"{rec_label}:unknown_candidate_id:{cid}")
                fabricated_outputs.append({"label": rec_label, "reason": "unknown_candidate_id", "candidate_id": cid})
                continue
            cand_idx = cand_by_id[cid]

        cand = batch_candidates[cand_idx]
        cand_cid = str(cand.get("candidate_id") or "").strip()
        if cand_idx in handled_indices:
            if cand_cid and not (allow_legacy_url_lookup and raw_cid is None):
                audit["duplicate_candidate_ids"].append(cand_cid)
                fabricated_outputs.append({"label": rec_label, "reason": "duplicate_candidate_id", "candidate_id": cand_cid})
            else:
                audit["duplicate_output_urls"].append(str(cand.get("url") or ""))
                fabricated_outputs.append({"label": rec_label, "reason": "duplicate_output_url", "original_url": raw_url})
            continue

        # Cross-field provenance mismatch check: reject instead of silently trusting contradictory metadata.
        if raw_url and not (allow_legacy_url_lookup and raw_cid is None):
            allowed_urls = {
                str(cand.get("url") or "").strip(),
                str(cand.get("url") or "").strip().rstrip("/"),
                str(cand.get("final_url") or "").strip(),
                str(cand.get("final_url") or "").strip().rstrip("/"),
            }
            allowed_urls.discard("")
            if raw_url not in allowed_urls and raw_url.rstrip("/") not in allowed_urls:
                handled_indices.add(cand_idx)
                audit["url_provenance_errors"].append(f"{rec_label}:mismatched_url_for_{cand_cid}:{raw_url}")
                rejected_records.append(
                    make_non_valid_entry(cand, batch_index, [f"url_mismatch_for_{cand_cid}:{raw_url}"], raw_model_record=rec)
                )
                continue

        got_domain = str(rec.get("publisher_domain") or "").strip().lower()
        exp_domain = str(cand.get("domain") or "").strip().lower()
        if got_domain and exp_domain and got_domain != exp_domain:
            handled_indices.add(cand_idx)
            audit["candidate_id_errors"].append(f"{rec_label}:mismatched_domain_for_{cand_cid}:{got_domain}")
            rejected_records.append(
                make_non_valid_entry(cand, batch_index, [f"domain_mismatch_for_{cand_cid}:{got_domain}"], raw_model_record=rec)
            )
            continue

        handled_indices.add(cand_idx)
        locked_rec, rejection_reasons = lock_and_validate_record(
            rec, cand, rec_label, batch_index, audit, allow_legacy_url_lookup=allow_legacy_url_lookup
        )
        if locked_rec is not None:
            validated_records.append(locked_rec)
        else:
            rejected_records.append(make_non_valid_entry(cand, batch_index, rejection_reasons, raw_model_record=rec))

    for idx, cand in enumerate(batch_candidates):
        if idx not in handled_indices:
            missing_cid = str(cand.get("candidate_id") or "").strip()
            missing_url = str(cand.get("url") or "").strip()
            if missing_cid:
                audit["missing_candidate_ids"].append(missing_cid)
            audit["missing_input_urls"].append(missing_url)
            rejected_records.append(
                make_non_valid_entry(cand, batch_index, [f"omitted_by_model_in_batch_{batch_index}"])
            )

    top_cids = parsed.get("top_5_valuable_candidate_ids")
    if isinstance(top_cids, list):
        for item in top_cids:
            scid = str(item or "").strip()
            if not isinstance(item, str) or not scid or scid not in cand_by_id:
                audit["candidate_id_errors"].append(f"top_5_unknown_candidate_id:{item}")

    top5 = parsed.get("top_5_valuable_urls")
    if isinstance(top5, list):
        for u in top5:
            su = str(u).strip()
            if su and su not in url_lookup and su.rstrip("/") not in url_lookup:
                audit["url_provenance_errors"].append(f"top_5_unknown_url:{su}")

    hard_error_buckets = (
        "missing_top_fields",
        "missing_record_fields",
        "candidate_id_errors",
        "missing_candidate_ids",
        "duplicate_candidate_ids",
        "url_provenance_errors",
        "missing_input_urls",
        "duplicate_output_urls",
        "verification_errors",
        "headline_only_inference_warnings",
        "language_errors",
        "geographic_errors",
        "source_type_errors",
    )
    total_errs = sum(len(audit[b]) for b in hard_error_buckets) + (1 if audit["truncated"] else 0)
    audit["total_error_count"] = total_errs
    audit["validation_passed"] = (not audit["malformed_json"]) and (total_errs == 0)

    return {
        "parsed": parsed,
        "audit": audit,
        "validated_records": validated_records,
        "rejected_or_incomplete_records": rejected_records,
        "fabricated_outputs": fabricated_outputs,
    }


def compute_accounting_summary(
    selected_candidates: list[dict[str, Any]],
    validated_records: list[dict[str, Any]],
    rejected_records: list[dict[str, Any]],
    unprocessed_records: list[dict[str, Any]],
) -> dict[str, Any]:
    total_input = len(selected_candidates)
    all_accounted = validated_records + rejected_records + unprocessed_records
    val_urls = [r["original_url"] for r in all_accounted]
    val_cids = [r.get("candidate_id", "") for r in all_accounted]
    input_urls = [str(c.get("url") or "").strip() for c in selected_candidates]
    input_cids = [str(c.get("candidate_id") or "").strip() for c in selected_candidates]
    url_match = (
        len(val_urls) == total_input
        and len(set(val_urls)) == len(set(input_urls))
        and set(val_urls) == set(input_urls)
    )
    cid_match = (
        all(bool(c) for c in val_cids)
        and len(set(val_cids)) == len(set(input_cids))
        and set(val_cids) == set(input_cids)
    )
    return {
        "total_input_candidates": total_input,
        "validated_count": len(validated_records),
        "rejected_or_incomplete_count": len(rejected_records),
        "unprocessed_count": len(unprocessed_records),
        "accounted_total": len(all_accounted),
        "accounting_check_passed": url_match and cid_match,
    }


def synthesize_consolidated_output(
    selected_candidates: list[dict[str, Any]],
    batch_outcomes: list[dict[str, Any]],
    validated_records: list[dict[str, Any]],
    rejected_records: list[dict[str, Any]],
    unprocessed_records: list[dict[str, Any]],
    fabricated_outputs: list[dict[str, Any]],
    meta_info: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Aggregate per-batch outputs into one audited dataset without extra LLM calls."""
    tier_counts = {tier: 0 for tier in ALLOWED_RELEVANCE_TIERS}
    access_counts = {"Partially verified": 0, "Not verified": 0}
    official_count = 0
    provenance_correction_count = 0
    relevance_downgrade_count = 0
    review_flagged_count = 0
    contradictory_summary_flag_count = 0

    for rec in validated_records:
        tier = rec["thesis_relevance_tier"]
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        acc = rec["page_access_verification_status"]
        access_counts[acc] = access_counts.get(acc, 0) + 1
        if rec.get("is_primary_official_document") is True:
            official_count += 1
        if rec.get("provenance_corrections"):
            provenance_correction_count += len(rec["provenance_corrections"])
        if rec.get("thesis_relevance_tier_model") != rec.get("thesis_relevance_tier"):
            relevance_downgrade_count += 1
        if rec.get("needs_review"):
            review_flagged_count += 1
        for rf in rec.get("review_flags") or []:
            if "contradicts" in rf or "underclaimed" in rf:
                contradictory_summary_flag_count += 1

    # Single-batch runs can retain model executive summary directly alongside counts
    single_parsed = batch_outcomes[0].get("parsed") if len(batch_outcomes) == 1 else None
    batch_exec_notes = [
        str(b["parsed"].get("executive_summary") or "").strip()
        for b in batch_outcomes
        if isinstance(b.get("parsed"), dict) and b["parsed"].get("executive_summary")
    ]
    if single_parsed and single_parsed.get("executive_summary"):
        exec_summary = str(single_parsed["executive_summary"]).strip()
    else:
        exec_summary = (
            f"Toplam {len(selected_candidates)} aday kaydın {len(validated_records)} adedi doğrulamadan geçti "
            f"(Doğrudan: {tier_counts['Direct']}, Dolaylı: {tier_counts['Indirect']}, "
            f"Arka plan: {tier_counts['Background']}, İlgisiz: {tier_counts['Irrelevant']}); "
            f"{len(rejected_records)} kayıt reddedildi/eksik işaretlendi, "
            f"{len(unprocessed_records)} kayıt işlenmeden ayrıldı. "
            f"Sayfa erişim doğrulaması yalnızca {access_counts['Partially verified']} kayıtta 'Partially verified' düzeyindedir; "
            f"hiçbir kayıt Antigravity birincil incelemesi olmadan 'Verified' sayılmamıştır."
        )

    top_map_by_url = {r["original_url"]: r for r in validated_records}
    top_map_by_cid = {r["candidate_id"]: r for r in validated_records if r.get("candidate_id")}
    raw_top_urls: list[str] = []
    for b in batch_outcomes:
        parsed = b.get("parsed") or {}
        for item in parsed.get("top_5_valuable_candidate_ids") or []:
            scid = str(item or "").strip()
            if scid in top_map_by_cid:
                mapped_url = top_map_by_cid[scid]["original_url"]
                if mapped_url not in raw_top_urls:
                    raw_top_urls.append(mapped_url)
        for u in parsed.get("top_5_valuable_urls") or []:
            su = str(u).strip()
            if su in top_map_by_url and su not in raw_top_urls:
                raw_top_urls.append(su)
    tier_priority = {"Direct": 0, "Indirect": 1, "Background": 2, "Irrelevant": 3}
    ver_priority = {"Partially verified": 0, "Not verified": 1}
    ranked_top = sorted(
        raw_top_urls,
        key=lambda u: (
            tier_priority.get(top_map_by_url[u]["thesis_relevance_tier"], 9),
            ver_priority.get(top_map_by_url[u]["page_access_verification_status"], 9),
        ),
    )[:5]
    ranked_top_cids = [top_map_by_url[u]["candidate_id"] for u in ranked_top]

    questions: list[str] = []
    priorities: list[str] = []
    for b in batch_outcomes:
        parsed = b.get("parsed") or {}
        for q in parsed.get("research_questions") or []:
            sq = str(q).strip()
            if sq and sq not in questions:
                questions.append(sq)
        for p in parsed.get("verification_priorities") or []:
            sp = str(p).strip()
            if sp and sp not in priorities:
                priorities.append(sp)

    official_gap_note = (
        str(single_parsed.get("official_document_gap_note")).strip()
        if single_parsed and single_parsed.get("official_document_gap_note")
        else (
            f"Havuzda resmî birincil belge sayısı `{official_count}` olarak tespit edilmiştir. "
            "Estonya/Letonya parlamento (Riigikogu, Saeima), bakanlık ve resmî mevzuat portalları "
            "(riigiteataja.ee, likumi.lv) haricî resmî doğrulama ile tamamlanmalıdır."
        )
    )

    accounting = compute_accounting_summary(
        selected_candidates, validated_records, rejected_records, unprocessed_records
    )

    consolidated = {
        "schema_version": "mistral_osint_v3_candidate_id",
        "meta": meta_info,
        "accounting": accounting,
        "summary_counts": {
            "relevance_tiers": tier_counts,
            "page_access_verification_statuses": access_counts,
            "primary_official_documents": official_count,
            "provenance_corrections_applied": provenance_correction_count,
            "relevance_overclaims_downgraded": relevance_downgrade_count,
            "review_flagged_records": review_flagged_count,
            "contradictory_summary_flags": contradictory_summary_flag_count,
        },
        "executive_summary": exec_summary,
        "batch_executive_notes": batch_exec_notes,
        "official_document_gap_note": official_gap_note,
        "validated_records": validated_records,
        "rejected_or_incomplete_records": rejected_records,
        "unprocessed_records": unprocessed_records,
        "fabricated_outputs_excluded": fabricated_outputs,
        "top_5_valuable_candidate_ids": ranked_top_cids,
        "top_5_valuable_urls": ranked_top,
        "research_questions": questions[:6],
        "verification_priorities": priorities[:8],
    }

    batch_audits = [b["audit"] for b in batch_outcomes]
    consolidated_audit = {
        "meta": meta_info,
        "accounting": accounting,
        "summary_counts": consolidated["summary_counts"],
        "total_batches_planned": meta_info.get("planned_batch_count", len(batch_outcomes)),
        "total_batches_executed": len(batch_outcomes),
        "batches_passed": sum(1 for a in batch_audits if a.get("validation_passed")),
        "batches_with_issues": sum(1 for a in batch_audits if not a.get("validation_passed")),
        "total_hard_errors": sum(int(a.get("total_error_count", 0)) for a in batch_audits),
        "total_provenance_corrections": provenance_correction_count,
        "total_relevance_downgrades": relevance_downgrade_count,
        "total_review_flagged_records": review_flagged_count,
        "total_contradictory_summary_flags": contradictory_summary_flag_count,
        "fabricated_output_count": len(fabricated_outputs),
        "batch_audits": batch_audits,
    }
    return consolidated, consolidated_audit


def render_consolidated_markdown(consolidated: dict[str, Any], audit: dict[str, Any]) -> str:
    meta = consolidated["meta"]
    acc = consolidated["accounting"]
    counts = consolidated["summary_counts"]
    tiers = counts["relevance_tiers"]
    lines = [
        f"# Mistral OSINT Güvenli İş Akışı Raporu ({meta.get('mode_label', 'Safeguarded Run')})",
        "",
        f"- **Çalıştırma Tarihi:** {meta.get('run_date')} (Pencere: {meta.get('start_date')} – {meta.get('end_date')})",
        f"- **Model (İstenen / Gerçekleşen):** `{meta.get('requested_model')}` / `{meta.get('actual_model')}`",
        f"- **Toplam Aday / Parti Sayısı:** `{acc['total_input_candidates']}` aday | `{audit['total_batches_executed']}/{audit['total_batches_planned']}` parti (parti boyutu=`{meta.get('batch_size')}`)",
        f"- **Token / Tahmini Maliyet:** prompt=`{meta.get('prompt_tokens')}`, completion=`{meta.get('completion_tokens')}`, total=`{meta.get('total_tokens')}` | maliyet=`${meta.get('estimated_cost_usd')}` (tavan=`${meta.get('max_total_cost_usd')}`)",
        f"- **Tam Aday Muhasebesi:** doğrulanan=`{acc['validated_count']}`, reddedilen/eksik=`{acc['rejected_or_incomplete_count']}`, işlenmeyen=`{acc['unprocessed_count']}` | invariant=`{'PASS' if acc['accounting_check_passed'] else 'FAIL'}`",
        f"- **Tez İlişki Dağılımı:** `Direct={tiers['Direct']}`, `Indirect={tiers['Indirect']}`, `Background={tiers['Background']}`, `Irrelevant={tiers['Irrelevant']}` (koruma düşürmesi=`{counts['relevance_overclaims_downgraded']}`)",
        f"- **İnceleme Gerektiren (Review Flag) / Çelişkili Özet:** `{counts.get('review_flagged_records', 0)}` kayıt / `{counts.get('contradictory_summary_flags', 0)}` bayrak",
        "",
        "## Genel Değerlendirme",
        consolidated["executive_summary"],
        "",
        "## Resmî Birincil Belge Boşluk Notu",
        consolidated["official_document_gap_note"],
        "",
        "---",
        "",
        f"## Doğrulanmış ve Provenansı Kilitli Kayıtlar ({len(consolidated['validated_records'])})",
        "",
    ]

    for idx, rec in enumerate(consolidated["validated_records"], start=1):
        cid_prefix = f"[{rec['candidate_id']}] " if rec.get("candidate_id") else ""
        lines.extend([
            f"### {idx}. {cid_prefix}{rec['original_title']}",
            f"- **Orijinal URL:** {rec['original_url']}",
        ])
        if rec.get("final_url") and rec["final_url"] != rec["original_url"]:
            lines.append(f"- **Yönlendirilmiş Son URL:** {rec['final_url']}")
        if rec.get("normalized_title"):
            lines.append(f"- **Model Başlık Önerisi (Ayrı Saklandı):** {rec['normalized_title']}")
        lines.extend([
            f"- **Yayıncı / Alan Adı / Ülke:** {rec['publisher']} (`{rec['publisher_domain']}`) / `{rec['publisher_country']}`",
            f"- **Yayın Dili / GDELT Kategorisi:** `{rec['publication_language']}` / `{rec['discovery_category']}`",
            f"- **Yayın Tarihi (Sayfa) / GDELT Görülme:** `{rec['publication_date']}` / `{rec['gdelt_seen_date']}` | **Olay Tarihi:** `{rec['event_date']}`",
            f"- **Kaynak Türü:** `{rec['source_type']}` | **Resmî Birincil Belge:** `{rec['is_primary_official_document']}`",
            f"- **Coğrafi İlişki:** {', '.join(rec.get('geographic_relevance') or [])}",
            f"- **Sayfa Erişim Doğrulaması:** `{rec['page_access_verification_status']}` ({rec['page_access_status'] or 'bilgi yok'})",
            f"- **Olgusal İddia Doğrulaması:** `{rec['factual_claim_verification_status']}`",
            f"- **Tez İlişki Düzeyi:** `{rec['thesis_relevance_tier']}` (`{rec['thesis_relevance_tier_tr']}`) — model={rec['thesis_relevance_tier_model']}",
        ])
        if rec.get("needs_review"):
            lines.append(f"- **İnceleme Bayrağı (`needs_review=True`):** `{', '.join(rec.get('review_flags') or [])}`")
        if rec.get("relevance_guardrail_note"):
            lines.append(f"- **Tez Koruma Notu:** {rec['relevance_guardrail_note']}")
        if rec.get("provenance_corrections"):
            lines.append(f"- **Provenans Kilidi Düzeltmeleri:** {', '.join(rec['provenance_corrections'])}")
        lines.extend([
            f"- **İçerik Özeti:** {rec['content_summary']}",
            f"- **Tezle İlişkisi:** {rec['thesis_relevance']}",
            f"- **Güvenilirlik Notu:** {rec['reliability_notes']}",
            "",
        ])

    if consolidated["rejected_or_incomplete_records"]:
        lines.extend(["---", "", f"## Reddedilen veya Eksik Kayıtlar ({len(consolidated['rejected_or_incomplete_records'])})", ""])
        for item in consolidated["rejected_or_incomplete_records"]:
            cpref = f"[{item['candidate_id']}] " if item.get("candidate_id") else ""
            lines.append(f"- **{cpref}{item['original_title']}** ({item['original_url']}) — Nedenler: `{', '.join(item['reasons'])}`")
        lines.append("")

    if consolidated["unprocessed_records"]:
        lines.extend(["---", "", f"## İşlenmeden Korunan Kayıtlar ({len(consolidated['unprocessed_records'])})", ""])
        for item in consolidated["unprocessed_records"]:
            cpref = f"[{item['candidate_id']}] " if item.get("candidate_id") else ""
            lines.append(f"- **{cpref}{item['original_title']}** ({item['original_url']}) — Nedenler: `{', '.join(item['reasons'])}`")
        lines.append("")

    lines.extend(["---", "", "## Tez İçin Öncelikli Bağlantılar"])
    for u in consolidated.get("top_5_valuable_urls") or []:
        lines.append(f"- {u}")

    lines.extend(["", "## Yeni Kaynaklara Dayalı Araştırma Soruları"])
    for q in consolidated.get("research_questions") or []:
        lines.append(f"- {q}")

    lines.extend(["", "## Antigravity Doğrulama Öncelikleri"])
    for p in consolidated.get("verification_priorities") or []:
        lines.append(f"- {p}")

    lines.extend([
        "",
        "---",
        "",
        "## Deterministik Denetim Özeti",
        f"- Yürütülen Parti: `{audit['total_batches_executed']}/{audit['total_batches_planned']}` | Tam Geçen Parti: `{audit['batches_passed']}` | Uyarılı Parti: `{audit['batches_with_issues']}`",
        f"- Toplam Hard Hata: `{audit['total_hard_errors']}`",
        f"- Provenans Kilidi Düzeltmesi: `{audit['total_provenance_corrections']}`",
        f"- Tez İlgi Aşırı-İddia Düşürmesi (`Direct` guard): `{audit['total_relevance_downgrades']}`",
        f"- İnceleme Bayrağı / Çelişkili Özet Sayısı: `{audit.get('total_review_flagged_records', 0)}` / `{audit.get('total_contradictory_summary_flags', 0)}`",
        f"- Uydurma / Eşleşmeyen Model Çıktısı Engeli: `{audit['fabricated_output_count']}`",
    ])
    return "\n".join(lines) + "\n"


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    rates = MODEL_PRICING_PER_M.get(model)
    if not rates:
        return None
    in_rate, out_rate = rates
    return round((prompt_tokens * in_rate + completion_tokens * out_rate) / 1_000_000.0, 8)


def estimate_worst_case_cost_usd(model: str, messages: list[dict[str, str]], max_tokens: int) -> tuple[int, float | None]:
    total_chars = sum(len(m.get("content", "")) for m in messages)
    est_prompt_tokens = max(1, int(total_chars / 2.5))
    rates = MODEL_PRICING_PER_M.get(model)
    if not rates:
        return est_prompt_tokens, None
    in_rate, out_rate = rates
    return est_prompt_tokens, round((est_prompt_tokens * in_rate + max_tokens * out_rate) / 1_000_000.0, 8)


def run_mistral_completion(
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    structured: bool,
) -> tuple[int, dict[str, Any], str]:
    """Send exactly ONE Mistral chat completion request (zero automatic retries)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.0 if structured else 0.1,
        "max_tokens": max_tokens,
    }
    if structured:
        payload["response_format"] = {"type": "json_object"}
    try:
        resp = requests.post(MISTRAL_ENDPOINT, headers=headers, json=payload, timeout=120)
    except requests.RequestException as exc:
        return 0, {}, redact(f"{type(exc).__name__}: {exc}", api_key)

    if resp.status_code != 200:
        return resp.status_code, {}, redact(resp.text[:800].replace("\n", " "), api_key)

    try:
        return resp.status_code, resp.json(), ""
    except ValueError as exc:
        return resp.status_code, {}, f"Invalid JSON response: {exc}"


def load_candidates_from_json(json_path: Path) -> tuple[str, str, str, list[dict[str, Any]], int]:
    raw_payload = json.loads(json_path.read_text(encoding="utf-8"))
    run_date = str(raw_payload.get("run_date") or datetime.now(TZ).date().isoformat())
    window = raw_payload.get("research_window") or {}
    start_date = str(window.get("start") or run_date)
    end_date = str(window.get("end") or run_date)
    candidates = list(raw_payload.get("candidates") or [])
    total_pool_count = int(raw_payload.get("total_pool_count") or len(candidates))
    return run_date, start_date, end_date, candidates, total_pool_count


def execute_batched_structured_pipeline(
    template: str,
    selected: list[dict[str, Any]],
    start_date: str,
    end_date: str,
    run_date: str,
    model: str,
    batch_size: int,
    max_tokens: int,
    max_total_cost: float,
    batch_dir: Path,
    completion_fn: Callable[[list[dict[str, str]], int, list[dict[str, Any]]], tuple[int, dict[str, Any], str]],
    mode_label: str,
    input_label: str,
    total_pool_count: int,
    is_sample: bool,
    stop_on_critical_provenance: bool = False,
    allow_legacy_url_lookup: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Process candidates sequentially in batches with cost ceiling, raw saving & full accounting."""
    selected = assign_candidate_ids(selected)
    batches = chunk_candidates(selected, batch_size)
    planned_batches = len(batches)
    cumulative_cost = 0.0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    actual_models_seen: set[str] = set()

    batch_outcomes: list[dict[str, Any]] = []
    validated_records: list[dict[str, Any]] = []
    rejected_records: list[dict[str, Any]] = []
    unprocessed_records: list[dict[str, Any]] = []
    fabricated_outputs: list[dict[str, Any]] = []
    halt_reason = ""
    exit_code = 0

    for idx, batch_cands in enumerate(batches, start=1):
        messages = build_structured_messages(
            template, batch_cands, start_date, end_date, run_date, batch_index=idx, total_batches=planned_batches
        )
        est_prompt, batch_worst_cost = estimate_worst_case_cost_usd(model, messages, max_tokens)

        if batch_worst_cost is not None and (cumulative_cost + batch_worst_cost) > max_total_cost:
            halt_reason = (
                f"max_total_cost_exceeded_before_batch_{idx}"
                f"(cum={cumulative_cost:.6f}+worst={batch_worst_cost:.6f}>{max_total_cost:.4f})"
            )
            logging.error("Maliyet tavani asilacak (%s). Kalan partiler durduruldu.", halt_reason)
            for rem_idx in range(idx - 1, planned_batches):
                for c in batches[rem_idx]:
                    unprocessed_records.append(make_non_valid_entry(c, rem_idx + 1, [halt_reason]))
            exit_code = 5
            break

        status, data, err = completion_fn(messages, idx, batch_cands)
        raw_batch_file = batch_dir / f"batch_{idx:02d}_raw.json"

        if status != 200:
            if idx == 1 and status == 0:
                halt_reason = f"batch_1_connection_error_http_0:{err}"
            else:
                halt_reason = f"batch_{idx}_api_error_http_{status}:{err}"
            logging.error("Parti %d basarisiz (otomatik yeniden deneme YOK): HTTP %s | %s", idx, status, err)
            if not (idx == 1 and status == 0):
                raw_batch_file.write_text(
                    json.dumps({"batch_index": idx, "http_status": status, "error": err}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            for rem_idx in range(idx - 1, planned_batches):
                for c in batches[rem_idx]:
                    unprocessed_records.append(make_non_valid_entry(c, rem_idx + 1, [halt_reason]))
            exit_code = 4
            break

        actual_model = str(data.get("model") or model)
        actual_models_seen.add(actual_model)
        choices = data.get("choices") or []
        content = ""
        finish_reason = ""
        if choices and isinstance(choices[0], dict):
            finish_reason = str(choices[0].get("finish_reason") or "")
            content = str((choices[0].get("message") or {}).get("content") or "").strip()

        usage = data.get("usage") or {}
        p_tok = int(usage.get("prompt_tokens") or 0)
        c_tok = int(usage.get("completion_tokens") or 0)
        t_tok = int(usage.get("total_tokens") or (p_tok + c_tok))
        batch_cost = estimate_cost_usd(actual_model, p_tok, c_tok) or 0.0

        cumulative_cost = round(cumulative_cost + batch_cost, 8)
        total_prompt_tokens += p_tok
        total_completion_tokens += c_tok

        raw_batch_file.write_text(
            json.dumps(
                {
                    "batch_index": idx,
                    "http_status": status,
                    "actual_model": actual_model,
                    "finish_reason": finish_reason,
                    "estimated_worst_case_prompt_tokens": est_prompt,
                    "usage": {"prompt_tokens": p_tok, "completion_tokens": c_tok, "total_tokens": t_tok},
                    "estimated_cost_usd": batch_cost,
                    "input_candidate_ids": [str(c.get("candidate_id") or "") for c in batch_cands],
                    "input_urls": [str(c.get("url") or "") for c in batch_cands],
                    "raw_content": content,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        outcome = validate_structured_batch(
            content,
            finish_reason,
            batch_cands,
            batch_index=idx,
            allow_legacy_url_lookup=allow_legacy_url_lookup,
        )
        outcome["audit"]["http_status"] = status
        outcome["audit"]["actual_model"] = actual_model
        outcome["audit"]["prompt_tokens"] = p_tok
        outcome["audit"]["completion_tokens"] = c_tok
        outcome["audit"]["total_tokens"] = t_tok
        outcome["audit"]["estimated_cost_usd"] = batch_cost
        outcome["audit"]["raw_batch_file"] = str(raw_batch_file)

        batch_outcomes.append(outcome)
        validated_records.extend(outcome["validated_records"])
        rejected_records.extend(outcome["rejected_or_incomplete_records"])
        fabricated_outputs.extend(outcome["fabricated_outputs"])

        logging.info(
            "Parti %d/%d tamamlandi: HTTP=%s finish=%s valid=%d rejected=%d err=%d cost=$%.6f cum=$%.6f",
            idx,
            planned_batches,
            status,
            finish_reason,
            len(outcome["validated_records"]),
            len(outcome["rejected_or_incomplete_records"]),
            outcome["audit"]["total_error_count"],
            batch_cost,
            cumulative_cost,
        )

        is_critical_provenance_failure = bool(
            outcome["audit"]["malformed_json"]
            or outcome["audit"]["url_provenance_errors"]
            or outcome["audit"]["candidate_id_errors"]
            or outcome["audit"]["duplicate_candidate_ids"]
        )
        if stop_on_critical_provenance and is_critical_provenance_failure:
            halt_reason = f"critical_provenance_failure_in_batch_{idx}"
            logging.error(
                "Kritik provenans dogrulama hatasi (parti %d). Kalan partiler durduruldu: malformed=%s url_errs=%d cid_errs=%d dup_cids=%d",
                idx,
                outcome["audit"]["malformed_json"],
                len(outcome["audit"]["url_provenance_errors"]),
                len(outcome["audit"]["candidate_id_errors"]),
                len(outcome["audit"]["duplicate_candidate_ids"]),
            )
            for rem_idx in range(idx, planned_batches):
                for c in batches[rem_idx]:
                    unprocessed_records.append(make_non_valid_entry(c, rem_idx + 1, [halt_reason]))
            exit_code = 6
            break

    meta_info = {
        "mode_label": mode_label,
        "run_date": run_date,
        "start_date": start_date,
        "end_date": end_date,
        "requested_model": model,
        "actual_model": ", ".join(sorted(actual_models_seen)) or model,
        "sample_mode": is_sample,
        "batch_size": batch_size,
        "max_tokens_per_batch": max_tokens,
        "planned_batch_count": planned_batches,
        "total_pool_count": total_pool_count,
        "selected_count": len(selected),
        "prompt_tokens": total_prompt_tokens,
        "completion_tokens": total_completion_tokens,
        "total_tokens": total_prompt_tokens + total_completion_tokens,
        "estimated_cost_usd": cumulative_cost,
        "max_total_cost_usd": max_total_cost,
        "retry_policy": "zero_automatic_retries",
        "halt_reason": halt_reason,
        "input_file": input_label,
        "raw_batch_dir": str(batch_dir),
    }

    consolidated, consolidated_audit = synthesize_consolidated_output(
        selected,
        batch_outcomes,
        validated_records,
        rejected_records,
        unprocessed_records,
        fabricated_outputs,
        meta_info,
    )
    return consolidated, consolidated_audit, exit_code


def build_dry_run_plan(
    template: str,
    selected: list[dict[str, Any]],
    start_date: str,
    end_date: str,
    run_date: str,
    model: str,
    batch_size: int,
    max_tokens: int,
    max_total_cost: float,
    input_label: str,
) -> dict[str, Any]:
    selected = assign_candidate_ids(selected)
    batches = chunk_candidates(selected, batch_size)
    planned = len(batches)
    batch_estimates: list[dict[str, Any]] = []
    total_est_prompt = 0
    total_worst_cost = 0.0

    for idx, batch_cands in enumerate(batches, start=1):
        messages = build_structured_messages(
            template, batch_cands, start_date, end_date, run_date, batch_index=idx, total_batches=planned
        )
        est_prompt, worst_cost = estimate_worst_case_cost_usd(model, messages, max_tokens)
        total_est_prompt += est_prompt
        total_worst_cost = round(total_worst_cost + (worst_cost or 0.0), 8)
        batch_estimates.append({
            "batch_index": idx,
            "candidate_ids": [str(c.get("candidate_id") or "") for c in batch_cands],
            "candidate_count": len(batch_cands),
            "est_prompt_tokens": est_prompt,
            "max_completion_tokens": max_tokens,
            "worst_case_cost_usd": worst_cost,
            "cumulative_worst_case_usd": total_worst_cost,
            "within_max_total_cost": total_worst_cost <= max_total_cost,
        })

    return {
        "mode": "dry_run_no_api_call",
        "input_file": input_label,
        "run_date": run_date,
        "model": model,
        "total_candidates": len(selected),
        "batch_size": batch_size,
        "planned_batches": planned,
        "max_tokens_per_batch": max_tokens,
        "total_est_prompt_tokens": total_est_prompt,
        "total_worst_case_completion_tokens": planned * max_tokens,
        "total_worst_case_cost_usd": total_worst_cost,
        "max_total_cost_usd": max_total_cost,
        "all_batches_fit_within_ceiling": total_worst_cost <= max_total_cost,
        "batches": batch_estimates,
    }


def run_self_test(template: str) -> int:
    """Zero-cost in-memory adversarial test suite covering all 10 production safeguards."""
    import tempfile

    assert MODEL_DEFAULT == "mistral-small-latest", f"Unexpected default model: {MODEL_DEFAULT}"
    assert BATCH_SIZE_DEFAULT == 6
    assert MAX_TOTAL_COST_DEFAULT == 0.10

    sample_cands = [
        {
            "title": "Latvia transitions Russian minority schools to Latvian language under new domestic reform ?",
            "url": "https://lsm.lv/en/article/sample-latvia-schools",
            "final_url": "https://lsm.lv/en/article/sample-latvia-schools",
            "domain": "lsm.lv",
            "language": "English",
            "source_country": "Latvia",
            "gdelt_seen_date": "20261122T090000Z",
            "discovery_category": "De-Russification",
            "page_access_status": "HTTP 200; HTML fetched (automated inspection only)",
            "page_title": "Latvia transitions Russian minority schools",
            "publisher_from_page": "LSM",
            "publication_date_from_page": "2026-11-22T08:30:00+02:00",
            "page_text_excerpt": "Saeima debated the state language transition for Russian-speaking minority schools in Latvia.",
            "http_status": 200,
        },
        {
            "title": "Nowa telewizja na Litwie – Kurier Wileński",
            "url": "https://kurierwilenski.lt/2026/11/20/nowa-telewizja-na-litwie/",
            "final_url": "https://kurierwilenski.lt/2026/11/20/nowa-telewizja-na-litwie/",
            "domain": "kurierwilenski.lt",
            "language": "Polish",
            "source_country": "Lithuania",
            "gdelt_seen_date": "20261122T120000Z",
            "discovery_category": "Lithuania",
            "page_access_status": "HTTP 200; HTML fetched (automated inspection only)",
            "page_title": "Nowa telewizja na Litwie – Kurier Wileński",
            "publisher_from_page": "Kurier Wileński",
            "publication_date_from_page": "2026-11-20T12:50:00+00:00",
            "page_text_excerpt": "Polish-language media project launched in Vilnius covering Lithuania minority community events.",
            "http_status": 200,
        },
        {
            "title": "Entrussifizierung in Lettland: Keine Denkmäler mehr für Puschkin",
            "url": "https://taz.de/Entrussifizierung-in-Lettland/!6000001/",
            "final_url": "",
            "domain": "taz.de",
            "language": "German",
            "source_country": "Germany",
            "gdelt_seen_date": "20261122T100000Z",
            "discovery_category": "De-Russification",
            "page_access_status": "",
            "page_title": "",
            "publisher_from_page": "",
            "publication_date_from_page": "",
            "page_text_excerpt": "",
        },
        {
            "title": "Ukraine drone strikes in Black Sea",
            "url": "https://news.day.az/world/1876203.html",
            "final_url": "https://news.day.az/world/1876203.html",
            "domain": "news.day.az",
            "language": "Russian",
            "source_country": "Azerbaijan",
            "gdelt_seen_date": "20261122T110000Z",
            "discovery_category": "Ukraine",
            "page_access_status": "HTTP 200; HTML fetched (automated inspection only)",
            "page_title": "Ukraine drone strikes in Black Sea",
            "publisher_from_page": "Day.Az",
            "publication_date_from_page": "2026-11-22T10:34:00+04:00",
            "page_text_excerpt": "Black Sea naval drones struck energy infrastructure near Novorossiysk.",
            "http_status": 200,
        },
        {
            "title": "Local basketball tournament results in Helsinki",
            "url": "https://yle.fi/a/74-00000001",
            "final_url": "https://yle.fi/a/74-00000001",
            "domain": "yle.fi",
            "language": "Finnish",
            "source_country": "Finland",
            "gdelt_seen_date": "20261122T070000Z",
            "discovery_category": "Regional actors",
            "page_access_status": "HTTP 200; HTML fetched (automated inspection only)",
            "page_title": "Local basketball",
            "publisher_from_page": "Yle",
            "publication_date_from_page": "2026-11-22T06:00:00+02:00",
            "page_text_excerpt": "Basketball league finals held in Helsinki on Saturday evening.",
            "http_status": 200,
        },
        {
            "title": "Adversarial Verified Override Attempt",
            "url": "https://444.hu/2026/11/22/test-verified",
            "final_url": "https://444.hu/2026/11/22/test-verified",
            "domain": "444.hu",
            "language": "Hungarian",
            "source_country": "Hungary",
            "gdelt_seen_date": "20261122T080000Z",
            "discovery_category": "Ukraine",
            "page_access_status": "HTTP 403; content not inspected",
            "page_title": "",
            "publisher_from_page": "",
            "publication_date_from_page": "",
            "page_text_excerpt": "",
            "http_status": 403,
        },
        # Batch 2 (4 items -> 1 omitted, 1 malformed/missing field, 2 will fail in batch 2 or cost guard)
        {
            "title": "Baltic grid synchronization update",
            "url": "https://err.ee/sample-grid-sync",
            "final_url": "https://err.ee/sample-grid-sync",
            "domain": "err.ee",
            "language": "English",
            "source_country": "Estonia",
            "gdelt_seen_date": "20261122T140000Z",
            "discovery_category": "Estonia",
            "page_access_status": "HTTP 200; HTML fetched (automated inspection only)",
            "page_title": "Baltic grid sync",
            "publisher_from_page": "ERR",
            "publication_date_from_page": "2026-11-22T14:00:00+02:00",
            "page_text_excerpt": "Estonia, Latvia and Lithuania synchronized electricity grids cleanly.",
            "http_status": 200,
        },
        {
            "title": "Omitted by model candidate test",
            "url": "https://juancole.com/2026/11/sample-omitted",
            "final_url": "https://juancole.com/2026/11/sample-omitted",
            "domain": "juancole.com",
            "language": "English",
            "source_country": "United States",
            "gdelt_seen_date": "20261122T150000Z",
            "discovery_category": "Regional actors",
            "page_access_status": "HTTP 200; HTML fetched (automated inspection only)",
            "page_title": "Omitted candidate",
            "publisher_from_page": "Informed Comment",
            "publication_date_from_page": "2026-11-22T15:00:00+00:00",
            "page_text_excerpt": "General essay on European energy security.",
            "http_status": 200,
        },
    ]

    sample_cands = assign_candidate_ids(sample_cands)

    # Test 1: Pure Candidate-ID mapping (zero URLs in model output), provenance restoration,
    # relevance guardrails, Verified/upgrade rejections, omitted preservation, and contradictory summary review flags.
    batch1_json = {
        "executive_summary": "Test ozeti",
        "official_document_gap_note": "Resmi belge yok",
        "top_5_valuable_candidate_ids": ["C001", "C999"],
        "research_questions": ["Soru 1?"],
        "verification_priorities": ["Oncelik 1"],
        "records": [
            {
                "candidate_id": "C001",
                "normalized_title": "Latvia transitions Russian minority schools under new domestic reform?",
                "publisher": "LSM Public Media",
                "event_date": "2026-11-22",
                "source_type": "News Article",
                "is_primary_official_document": False,
                "geographic_relevance": ["Latvia"],
                "page_access_verification_status": "Partially verified",
                "factual_claim_verification_status": "Pending Antigravity primary inspection",
                "content_summary": "Letonya Parlamentosu Rusça konuşulan azınlık okullarının Letoncaya geçiş reformunu görüştü.",
                "thesis_relevance_tier": "Direct",
                "thesis_relevance": "Letonya'da eğitim ve dil de-Rusifikasyonu.",
                "reliability_notes": "LSM kamu yayıncısıdır; birincil Saeima metni doğrulanmalıdır.",
            },
            {
                "candidate_id": "C002",
                "normalized_title": "Nowa telewizja na Litwie",
                "publisher": "Kurier Wilenski",
                "event_date": "Belirlenemedi",
                "source_type": "News Article",
                "is_primary_official_document": "false",
                "geographic_relevance": ["Lithuania"],
                "page_access_verification_status": "Partially verified",
                "content_summary": "Vilnius'ta Lehçe yayın yapan medya projesi başlatıldı.",
                "thesis_relevance_tier": "Direct",  # Overclaim -> downgraded to Indirect
                "thesis_relevance": "Bölgedeki azınlık medya politikaları bağlamında doğrudan önemlidir.",
                "reliability_notes": "Lehçe azınlık gazetesi.",
            },
            {
                "candidate_id": "C003",
                "publisher": "taz",
                "publication_date": "2026-11-22",  # Wrong date -> restored to Belirlenemedi
                "event_date": "Belirlenemedi",
                "source_type": "News Article",
                "is_primary_official_document": False,
                "geographic_relevance": ["Latvia"],
                "page_access_verification_status": "Not verified",
                "content_summary": "Sayfa içeriğine otomatik erişilemedi; yalnızca GDELT başlığı mevcuttur.",
                "thesis_relevance_tier": "Direct",
                "thesis_relevance": "Başlık düzeyinde Letonya de-Rusifikasyonu.",
                "reliability_notes": "İçerik erişimi Antigravity tarafından yapılmalıdır.",
            },
            {
                "candidate_id": "C004",
                "publisher": "Day.Az",
                "event_date": "2026-11-22",
                "source_type": "News Article",
                "is_primary_official_document": False,
                "geographic_relevance": ["Ukraine", "Russia"],
                "page_access_verification_status": "Not verified",  # Contradicts HTTP 200 excerpt (vesti.ru/aif.ru bug)!
                "content_summary": "Sayfa içeriğine otomatik erişilemedi; yalnızca GDELT başlığı mevcuttur",
                "thesis_relevance_tier": "Direct",  # Overclaim -> Background
                "thesis_relevance": "Genel savaş bağlamı.",
                "reliability_notes": "Azerbaycan portalı.",
            },
            {
                "candidate_id": "C005",
                "publisher": "Yle",
                "event_date": "2026-11-22",
                "source_type": "News Article",
                "is_primary_official_document": False,
                "geographic_relevance": ["Finland"],
                "page_access_verification_status": "Partially verified",
                "content_summary": "Helsinki'de basketbol maçı oynandı.",
                "thesis_relevance_tier": "Irrelevant",
                "thesis_relevance": "Tez sorularıyla bağlantısı yoktur.",
                "reliability_notes": "Yle spor haberi.",
            },
            {
                "candidate_id": "C006",
                "publisher": "444.hu",
                "event_date": "Belirlenemedi",
                "source_type": "News Article",
                "is_primary_official_document": False,
                "geographic_relevance": ["Hungary"],
                "page_access_verification_status": "Verified",  # Forbidden -> rejected
                "content_summary": "Sayfa içeriğine otomatik erişilemedi.",
                "thesis_relevance_tier": "Background",
                "thesis_relevance": "İlgisiz test.",
                "reliability_notes": "Test.",
            },
            {
                "candidate_id": "C999",  # Unknown candidate_id -> excluded/rejected cleanly
                "publisher": "Fake",
                "event_date": "Belirlenemedi",
                "source_type": "News Article",
                "is_primary_official_document": False,
                "geographic_relevance": ["Latvia"],
                "page_access_verification_status": "Not verified",
                "content_summary": "Fake.",
                "thesis_relevance_tier": "Background",
                "thesis_relevance": "Fake.",
                "reliability_notes": "Fake.",
            },
        ],
    }

    batch2_json = {
        "executive_summary": "Batch 2",
        "official_document_gap_note": "Yok",
        "top_5_valuable_candidate_ids": [],
        "research_questions": [],
        "verification_priorities": [],
        "records": [
            {
                "candidate_id": "C007",
                "publisher": "ERR",
                "event_date": "2026-11-22",
                "source_type": "Government/Official Document",  # Misclassified news site -> rejected
                "is_primary_official_document": True,
                "geographic_relevance": ["Estonia", "Latvia", "Lithuania"],
                "page_access_verification_status": "Partially verified",
                "content_summary": "Baltık elektrik şebekesi senkronize edildi.",
                "thesis_relevance_tier": "Indirect",
                "thesis_relevance": "Bölgesel altyapı.",
                "reliability_notes": "ERR kamu medyasıdır.",
            }
            # C008 (juancole.com) omitted -> rejected_or_incomplete with omitted_by_model
        ],
    }

    calls = 0

    def mock_completion(_m: list[dict[str, str]], b_idx: int, _b_cands: list[dict[str, Any]]) -> tuple[int, dict[str, Any], str]:
        nonlocal calls
        calls += 1
        payload_obj = batch1_json if b_idx == 1 else batch2_json
        return (
            200,
            {
                "model": "mistral-small-latest",
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload_obj, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 900, "total_tokens": 2100},
            },
            "",
        )

    with tempfile.TemporaryDirectory() as tmp:
        bdir = Path(tmp) / "batches"
        bdir.mkdir(parents=True, exist_ok=True)
        consolidated, audit, exit_code = execute_batched_structured_pipeline(
            template=template,
            selected=sample_cands,
            start_date="2026-11-16",
            end_date="2026-11-22",
            run_date="2026-11-22",
            model="mistral-small-latest",
            batch_size=6,
            max_tokens=4500,
            max_total_cost=0.10,
            batch_dir=bdir,
            completion_fn=mock_completion,
            mode_label="self_test",
            input_label="in-memory-self-test",
            total_pool_count=len(sample_cands),
            is_sample=True,
        )

        assert exit_code == 0
        assert calls == 2, f"Expected 2 batch calls, got {calls}"
        assert consolidated["accounting"]["accounting_check_passed"] is True
        assert consolidated["accounting"]["accounted_total"] == len(sample_cands)
        assert len(consolidated["validated_records"]) == 5
        assert len(consolidated["rejected_or_incomplete_records"]) == 3
        assert len(consolidated["fabricated_outputs_excluded"]) == 1
        assert consolidated["unprocessed_records"] == []

        by_cid = {r["candidate_id"]: r for r in consolidated["validated_records"]}
        assert by_cid["C001"]["original_url"] == sample_cands[0]["url"]
        assert by_cid["C001"]["original_title"] == sample_cands[0]["title"]
        assert by_cid["C001"]["normalized_title"] == "Latvia transitions Russian minority schools under new domestic reform?"
        assert by_cid["C001"]["factual_claim_verification_status"] == FACTUAL_CLAIM_VERIFICATION_DEFAULT
        assert by_cid["C002"]["is_primary_official_document"] is False
        assert by_cid["C002"]["thesis_relevance_tier"] == "Indirect"
        assert by_cid["C003"]["publication_date"] == "Belirlenemedi"
        assert by_cid["C003"]["page_access_verification_status"] == "Not verified"
        assert by_cid["C004"]["page_access_verification_status"] == "Partially verified"
        assert by_cid["C004"]["needs_review"] is True
        assert "summary_contradicts_extracted_page_text" in by_cid["C004"]["review_flags"]
        assert any("model_status_underclaimed_vs_input_evidence" in f for f in by_cid["C004"]["review_flags"])
        assert by_cid["C004"]["thesis_relevance_tier"] == "Background"
        assert by_cid["C005"]["thesis_relevance_tier"] == "Irrelevant"
        assert consolidated["top_5_valuable_candidate_ids"] == ["C001"]
        assert consolidated["top_5_valuable_urls"] == [sample_cands[0]["url"]]
        assert (bdir / "batch_01_raw.json").exists()
        assert (bdir / "batch_02_raw.json").exists()

    # Test 2: Zero-retry failure + Cost-ceiling halt accounting test
    fail_calls = 0

    def mock_http_429(_m: list[dict[str, str]], _b: int, _c: list[dict[str, Any]]) -> tuple[int, dict[str, Any], str]:
        nonlocal fail_calls
        fail_calls += 1
        return 429, {}, "rate_limited_test"

    with tempfile.TemporaryDirectory() as tmp2:
        bdir2 = Path(tmp2) / "batches_fail"
        bdir2.mkdir(parents=True, exist_ok=True)
        cons2, _aud2, code2 = execute_batched_structured_pipeline(
            template=template,
            selected=sample_cands,
            start_date="2026-11-16",
            end_date="2026-11-22",
            run_date="2026-11-22",
            model="mistral-small-latest",
            batch_size=3,
            max_tokens=4500,
            max_total_cost=0.10,
            batch_dir=bdir2,
            completion_fn=mock_http_429,
            mode_label="self_test_fail",
            input_label="in-memory-fail",
            total_pool_count=len(sample_cands),
            is_sample=True,
        )
        assert fail_calls == 1, f"Retries must be disabled (expected 1 call, got {fail_calls})"
        assert code2 == 4
        assert cons2["accounting"]["accounting_check_passed"] is True
        assert len(cons2["unprocessed_records"]) == len(sample_cands)

    with tempfile.TemporaryDirectory() as tmp3:
        bdir3 = Path(tmp3) / "batches_cost"
        bdir3.mkdir(parents=True, exist_ok=True)
        cost_calls = 0

        def never_called(_m: list[dict[str, str]], _b: int, _c: list[dict[str, Any]]) -> tuple[int, dict[str, Any], str]:
            nonlocal cost_calls
            cost_calls += 1
            return 200, {}, ""

        cons3, _aud3, code3 = execute_batched_structured_pipeline(
            template=template,
            selected=sample_cands,
            start_date="2026-11-16",
            end_date="2026-11-22",
            run_date="2026-11-22",
            model="mistral-small-latest",
            batch_size=6,
            max_tokens=4500,
            max_total_cost=0.0005,
            batch_dir=bdir3,
            completion_fn=never_called,
            mode_label="self_test_cost",
            input_label="in-memory-cost",
            total_pool_count=len(sample_cands),
            is_sample=True,
        )
        assert cost_calls == 0
        assert code3 == 5
        assert cons3["accounting"]["accounting_check_passed"] is True
        assert len(cons3["unprocessed_records"]) == len(sample_cands)

    # Test 3: Adversarial candidate_id errors (unknown, duplicate, missing) + URL mismatch + immediate halt
    prov_calls = 0

    def mock_bad_ids_batch1(_m: list[dict[str, str]], b_idx: int, b_cands: list[dict[str, Any]]) -> tuple[int, dict[str, Any], str]:
        nonlocal prov_calls
        prov_calls += 1
        recs = [
            {
                "candidate_id": b_cands[0]["candidate_id"],
                "publisher": "Pub",
                "event_date": "Belirlenemedi",
                "source_type": "News Article",
                "is_primary_official_document": False,
                "geographic_relevance": ["Latvia"],
                "page_access_verification_status": expected_page_access_verification_status(b_cands[0]),
                "content_summary": "Özet 1.",
                "thesis_relevance_tier": "Background",
                "thesis_relevance": "Test.",
                "reliability_notes": "Test.",
            },
            {
                "candidate_id": b_cands[0]["candidate_id"],  # Duplicate ID!
                "publisher": "Pub",
                "event_date": "Belirlenemedi",
                "source_type": "News Article",
                "is_primary_official_document": False,
                "geographic_relevance": ["Latvia"],
                "page_access_verification_status": expected_page_access_verification_status(b_cands[0]),
                "content_summary": "Özet dup.",
                "thesis_relevance_tier": "Background",
                "thesis_relevance": "Test.",
                "reliability_notes": "Test.",
            },
            {
                "candidate_id": "",  # Missing ID!
                "publisher": "Fake",
                "event_date": "Belirlenemedi",
                "source_type": "News Article",
                "is_primary_official_document": False,
                "geographic_relevance": ["Latvia"],
                "page_access_verification_status": "Not verified",
                "content_summary": "Missing.",
                "thesis_relevance_tier": "Background",
                "thesis_relevance": "Test.",
                "reliability_notes": "Test.",
            },
            {
                "candidate_id": b_cands[1]["candidate_id"],
                "original_url": "https://mutated.example.org/94601939.html",  # Cross-field URL mismatch!
                "publisher": "Fake",
                "event_date": "Belirlenemedi",
                "source_type": "News Article",
                "is_primary_official_document": False,
                "geographic_relevance": ["Lithuania"],
                "page_access_verification_status": "Partially verified",
                "content_summary": "Mismatch.",
                "thesis_relevance_tier": "Background",
                "thesis_relevance": "Test.",
                "reliability_notes": "Test.",
            },
        ]
        payload_obj = {
            "executive_summary": f"Test batch {b_idx}",
            "official_document_gap_note": "Yok",
            "records": recs,
            "top_5_valuable_candidate_ids": ["C888"],
            "research_questions": [],
            "verification_priorities": [],
        }
        return (
            200,
            {
                "model": "mistral-small-latest",
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload_obj, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 400, "total_tokens": 900},
            },
            "",
        )

    with tempfile.TemporaryDirectory() as tmp4:
        bdir4 = Path(tmp4) / "batches_crit"
        bdir4.mkdir(parents=True, exist_ok=True)
        cons4, aud4, code4 = execute_batched_structured_pipeline(
            template=template,
            selected=sample_cands,
            start_date="2026-11-16",
            end_date="2026-11-22",
            run_date="2026-11-22",
            model="mistral-small-latest",
            batch_size=4,
            max_tokens=4500,
            max_total_cost=0.10,
            batch_dir=bdir4,
            completion_fn=mock_bad_ids_batch1,
            mode_label="self_test_critical_prov",
            input_label="in-memory-crit",
            total_pool_count=len(sample_cands),
            is_sample=True,
            stop_on_critical_provenance=True,
        )
        assert prov_calls == 1, f"Expected immediate halt after Batch 1 (calls={prov_calls})"
        assert code4 == 6, f"Expected exit code 6, got {code4}"
        assert cons4["accounting"]["accounting_check_passed"] is True
        assert len(cons4["validated_records"]) == 1
        assert len(cons4["rejected_or_incomplete_records"]) == 3
        assert len(cons4["fabricated_outputs_excluded"]) == 2
        assert len(cons4["unprocessed_records"]) == 4
        assert aud4["batch_audits"][0]["duplicate_candidate_ids"] == ["C001"]
        assert any("missing_or_invalid_candidate_id" in e for e in aud4["batch_audits"][0]["candidate_id_errors"])
        assert any("top_5_unknown_candidate_id:C888" in e for e in aud4["batch_audits"][0]["candidate_id_errors"])
        assert any("mismatched_url_for_C002" in e for e in aud4["batch_audits"][0]["url_provenance_errors"])
        assert all("critical_provenance_failure_in_batch_1" in r["reasons"] for r in cons4["unprocessed_records"])

    print("SELF_TEST_PASSED: All candidate_id, provenance, consistency, guardrail, accounting, zero-retry, and cost-ceiling assertions passed (0 failures).")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Isolated Mistral OSINT source discovery & safeguarded batch workflow")
    parser.add_argument("--input-json", type=Path, help="Existing GDELT candidates JSON file to reuse")
    parser.add_argument("--sample-size", type=int, default=0, help="Process only a deterministic sample of N candidates (0 = all)")
    parser.add_argument("--model", type=str, default=os.getenv("MISTRAL_MODEL", MODEL_DEFAULT), help="Mistral model ID")
    parser.add_argument("--live-gdelt", action="store_true", help="Fetch fresh GDELT candidates instead of --input-json")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE_DEFAULT, help="Candidates per Mistral batch (default 6; recommended 5-8)")
    parser.add_argument("--max-tokens", type=int, default=MAX_TOKENS_DEFAULT, help="Max completion tokens per batch")
    parser.add_argument("--structured", action="store_true", help="Use strict JSON structured output + deterministic safeguards")
    parser.add_argument("--max-total-cost", type=float, default=MAX_TOTAL_COST_DEFAULT, help="Maximum cumulative worst-case USD cost ceiling (default 0.10)")
    parser.add_argument("--max-est-cost", type=float, default=None, help="Deprecated alias capping --max-total-cost if smaller")
    parser.add_argument("--self-test", action="store_true", help="Run offline adversarial unit tests with zero API calls")
    parser.add_argument("--replay-json", type=Path, help="Replay a saved Mistral structured JSON locally with zero API calls")
    parser.add_argument("--dry-run", action="store_true", help="Compute batch layout and worst-case cost ceiling locally with zero API calls")
    parser.add_argument("--stop-on-critical-provenance", action="store_true", help="Halt remaining batches immediately if malformed JSON or URL provenance failure occurs")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not PROMPT_FILE.exists():
        print(f"Prompt dosyasi bulunamadi: {PROMPT_FILE}", file=sys.stderr)
        return 2
    template = PROMPT_FILE.read_text(encoding="utf-8")

    if args.self_test:
        return run_self_test(template)

    mode_tag = "replay" if args.replay_json else ("dryrun" if args.dry_run else "run")
    setup_logging(mode_tag=mode_tag)

    model = args.model.strip() or MODEL_DEFAULT
    model_slug = slugify_model(model)
    max_total_cost = float(args.max_total_cost)
    if args.max_est_cost is not None:
        max_total_cost = min(max_total_cost, float(args.max_est_cost))

    if args.live_gdelt:
        if args.dry_run or args.replay_json:
            logging.error("--dry-run ve --replay-json yerel --input-json ile calisir; --live-gdelt ile kullanilamaz.")
            return 2
        start_utc, end_utc, start_date, end_date = date_window()
        run_date = end_date
        collected: dict[str, dict[str, Any]] = {}
        for category, query in SEARCHES:
            found = gdelt_search(query, start_utc, end_utc, category)
            logging.info("%s sorgusu: %d aday", category, len(found))
            for item in found:
                key = normalize_url(item["url"])
                if key and key not in collected:
                    collected[key] = item
            time.sleep(0.8)
        all_candidates = list(collected.values())[:MAX_UNIQUE_CANDIDATES]
        inspected: list[dict[str, Any]] = []
        for idx, item in enumerate(all_candidates[:MAX_PAGES_TO_INSPECT], start=1):
            inspected.append(inspect_page(item))
            if idx % 10 == 0:
                logging.info("Sayfa kontrolu: %d/%d", idx, min(MAX_PAGES_TO_INSPECT, len(all_candidates)))
            time.sleep(0.2)
        inspected.extend(all_candidates[MAX_PAGES_TO_INSPECT:])
        input_label = "live GDELT DOC 2.0 API"
        total_pool_count = len(inspected)
    elif args.input_json:
        if not args.input_json.exists():
            logging.error("Girdi JSON dosyasi bulunamadi: %s", args.input_json)
            return 2
        run_date, start_date, end_date, inspected, total_pool_count = load_candidates_from_json(args.input_json)
        input_label = str(args.input_json)
    else:
        logging.error("--input-json veya --live-gdelt seceneklerinden biri belirtilmelidir.")
        return 2

    if not inspected:
        logging.error("Islenecek aday bulunamadi.")
        return 3

    inspected = assign_candidate_ids(inspected)
    is_sample = (args.sample_size > 0 and args.sample_size < len(inspected)) or ("sample" in str(args.input_json or "").lower())
    selected = select_stratified_sample(inspected, args.sample_size) if (args.sample_size > 0 and args.sample_size < len(inspected)) else inspected
    scope_tag = "sample" if is_sample else "full"

    if args.dry_run:
        plan = build_dry_run_plan(
            template=template,
            selected=selected,
            start_date=start_date,
            end_date=end_date,
            run_date=run_date,
            model=model,
            batch_size=args.batch_size,
            max_tokens=args.max_tokens,
            max_total_cost=max_total_cost,
            input_label=input_label,
        )
        dryrun_out = output_path(f"{model_slug}_{scope_tag}_dryrun", run_date, "json")
        dryrun_out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info(
            "DRY_RUN_READY: candidates=%d batches=%d worst_case_cost=$%.6f ceiling=$%.4f within_ceiling=%s file=%s",
            plan["total_candidates"],
            plan["planned_batches"],
            plan["total_worst_case_cost_usd"],
            plan["max_total_cost_usd"],
            plan["all_batches_fit_within_ceiling"],
            dryrun_out,
        )
        return 0 if plan["all_batches_fit_within_ceiling"] else 5

    if args.replay_json:
        if not args.replay_json.exists():
            logging.error("Replay JSON dosyasi bulunamadi: %s", args.replay_json)
            return 2
        saved_raw_content = args.replay_json.read_text(encoding="utf-8")
        prefix_base = f"{model_slug}_{scope_tag}_local_validation"
        batch_dir = allocate_batch_dir(prefix_base, run_date)

        def replay_completion(_m: list[dict[str, str]], b_idx: int, _b_cands: list[dict[str, Any]]) -> tuple[int, dict[str, Any], str]:
            if b_idx != 1:
                return 400, {}, "Replay only supports matching batch size covering saved JSON"
            return (
                200,
                {
                    "model": model,
                    "choices": [{"finish_reason": "stop", "message": {"content": saved_raw_content}}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                },
                "",
            )

        effective_batch_size = max(len(selected), args.batch_size)
        is_legacy_replay = '"candidate_id"' not in saved_raw_content
        consolidated, audit, exit_code = execute_batched_structured_pipeline(
            template=template,
            selected=selected,
            start_date=start_date,
            end_date=end_date,
            run_date=run_date,
            model=model,
            batch_size=effective_batch_size,
            max_tokens=args.max_tokens,
            max_total_cost=max_total_cost,
            batch_dir=batch_dir,
            completion_fn=replay_completion,
            mode_label="Local Replay Validation (0 API call)",
            input_label=f"{input_label} | replay={args.replay_json}",
            total_pool_count=total_pool_count,
            is_sample=is_sample,
            allow_legacy_url_lookup=is_legacy_replay,
        )
        out_json = output_path(prefix_base, run_date, "json")
        out_audit = output_path(f"{prefix_base}_audit", run_date, "json")
        out_md = output_path(prefix_base, run_date, "md")
        out_json.write_text(json.dumps(consolidated, ensure_ascii=False, indent=2), encoding="utf-8")
        out_audit.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        out_md.write_text(render_consolidated_markdown(consolidated, audit), encoding="utf-8")
        logging.info(
            "REPLAY_VALIDATION_DONE: accounting=%s validated=%d rejected=%d downgrades=%d files=(%s, %s, %s)",
            consolidated["accounting"]["accounting_check_passed"],
            consolidated["accounting"]["validated_count"],
            consolidated["accounting"]["rejected_or_incomplete_count"],
            consolidated["summary_counts"]["relevance_overclaims_downgraded"],
            out_json,
            out_audit,
            out_md,
        )
        return exit_code

    # Live Mistral API execution requires key and refuses silent defaults
    load_dotenv(ENV_FILE)
    api_key = os.getenv("MISTRAL_API_KEY", "").strip()
    if not api_key:
        logging.error("MISTRAL_API_KEY bulunamadi. automation/.env dosyasini kontrol edin.")
        return 2

    prefix_base = f"{model_slug}_{scope_tag}"
    if args.structured:
        batch_dir = allocate_batch_dir(prefix_base, run_date)

        def live_completion(messages: list[dict[str, str]], _b_idx: int, _b_cands: list[dict[str, Any]]) -> tuple[int, dict[str, Any], str]:
            return run_mistral_completion(api_key, model, messages, max_tokens=args.max_tokens, structured=True)

        consolidated, audit, exit_code = execute_batched_structured_pipeline(
            template=template,
            selected=selected,
            start_date=start_date,
            end_date=end_date,
            run_date=run_date,
            model=model,
            batch_size=args.batch_size,
            max_tokens=args.max_tokens,
            max_total_cost=max_total_cost,
            batch_dir=batch_dir,
            completion_fn=live_completion,
            mode_label="Structured Safeguarded Batched Run",
            input_label=input_label,
            total_pool_count=total_pool_count,
            is_sample=is_sample,
            stop_on_critical_provenance=args.stop_on_critical_provenance,
        )
        if exit_code == 4 and consolidated["meta"]["halt_reason"].startswith("batch_1_connection_error_http_0"):
            shutil.rmtree(batch_dir, ignore_errors=True)
            logging.error("Ilk partide ag/baglanti hatasi (HTTP 0); bos cikti olusturulmadan durduruldu.")
            return 4
        out_json = output_path(f"{prefix_base}_structured", run_date, "json")
        out_audit = output_path(f"{prefix_base}_audit", run_date, "json")
        out_md = output_path(f"{prefix_base}_raw", run_date, "md")
        consolidated["meta"]["structured_json_file"] = str(out_json)
        consolidated["meta"]["audit_file"] = str(out_audit)
        consolidated["meta"]["report_file"] = str(out_md)
        out_json.write_text(json.dumps(consolidated, ensure_ascii=False, indent=2), encoding="utf-8")
        out_audit.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        out_md.write_text(render_consolidated_markdown(consolidated, audit), encoding="utf-8")
        logging.info(
            "RUN_COMPLETE: accounting=%s validated=%d rejected=%d unprocessed=%d cost=$%.6f files=(%s, %s, %s)",
            consolidated["accounting"]["accounting_check_passed"],
            consolidated["accounting"]["validated_count"],
            consolidated["accounting"]["rejected_or_incomplete_count"],
            consolidated["accounting"]["unprocessed_count"],
            consolidated["meta"]["estimated_cost_usd"],
            out_json,
            out_audit,
            out_md,
        )
        return exit_code

    # Legacy single-call unstructured path kept strictly below max_total_cost
    prompt = build_model_prompt(template, selected, start_date, end_date, run_date, is_sample=is_sample)
    messages = [{"role": "user", "content": prompt}]
    _, worst_est = estimate_worst_case_cost_usd(model, messages, args.max_tokens)
    if worst_est is not None and worst_est > max_total_cost:
        logging.error("Tahmini ust maliyet ($%s) siniri ($%s) asiyor. Istek gonderilmedi.", worst_est, max_total_cost)
        return 5
    status, data, err = run_mistral_completion(api_key, model, messages, max_tokens=args.max_tokens, structured=False)
    if status != 200:
        logging.error("Mistral API cagrisi basarisiz (otomatik yeniden deneme YOK): HTTP %s | %s", status, err)
        return 4
    actual_model = str(data.get("model") or model)
    choices = data.get("choices") or []
    report_content = str(((choices[0] or {}).get("message") or {}).get("content") or "").strip() if choices else ""
    finish_reason = str((choices[0] or {}).get("finish_reason") or "") if choices else ""
    usage = data.get("usage") or {}
    p_tok, c_tok = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    t_tok = int(usage.get("total_tokens") or (p_tok + c_tok))
    est_cost = estimate_cost_usd(actual_model, p_tok, c_tok)
    _, unmatched = audit_urls(report_content, selected)
    report_out = output_path(f"{prefix_base}_raw", run_date, "md")
    header = (
        f"# Mistral OSINT Source Discovery ({'Sample Run' if is_sample else 'Full Run'})\n\n"
        f"- Çalıştırma tarihi: {run_date}\n"
        f"- Model: `{model}` / `{actual_model}` | Finish Reason: `{finish_reason}`\n"
        f"- Token: prompt=`{p_tok}`, completion=`{c_tok}`, total=`{t_tok}` | Tahmini maliyet (USD): `{est_cost}`\n"
        f"- URL denetimi: `{'PASS' if not unmatched else f'WARN ({len(unmatched)})'}`\n\n---\n\n"
    )
    report_out.write_text(header + report_content + "\n", encoding="utf-8")
    logging.info("HTTP_STATUS: %s | TOKENS: total=%d | REPORT: %s", status, t_tok, report_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
