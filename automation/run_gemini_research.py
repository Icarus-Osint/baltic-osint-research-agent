"""Collect recent Baltic OSINT candidates from GDELT and analyze them with Gemini Free Tier.

This is a discovery-and-triage workflow, NOT a Deep Research replacement.
It does not claim independent verification of source claims. Antigravity should
open and verify the selected original pages in the next stage.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai

ROOT = Path(__file__).resolve().parents[1]
AUTOMATION_DIR = ROOT / "automation"
INBOX_DIR = ROOT / "02_Raw_Data" / "Gemini_Inbox"
PROMPT_FILE = ROOT / "00_Rules" / "free_tier_source_discovery_prompt.md"
LOG_DIR = AUTOMATION_DIR / "logs"
ENV_FILE = AUTOMATION_DIR / ".env"
TZ = ZoneInfo("Europe/Istanbul")
MODEL_DEFAULT = "gemini-3.1-flash-lite"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
USER_AGENT = "Mozilla/5.0 (compatible; OSINTResearchStudent/1.0)"
MAX_PER_QUERY = 35
MAX_UNIQUE_CANDIDATES = 90
MAX_PAGES_TO_INSPECT = 60
PAGE_TEXT_LIMIT = 1800

SEARCHES = [
    ("Estonia", '(Estonia OR Eesti OR Estonian) (language OR education OR citizenship OR media OR minority OR parliament OR security OR defence OR defense OR election OR Russian)'),
    ("Latvia", '(Latvia OR Latvian OR Latvija) (language OR education OR citizenship OR media OR minority OR parliament OR security OR defence OR defense OR election OR Russian)'),
    ("Lithuania", '(Lithuania OR Lithuanian OR Lietuva) (parliament OR government OR defence OR defense OR security OR energy OR sanctions OR NATO OR Russia)'),
    ("Russia", '(Russia OR Russian) (Estonia OR Latvia OR Lithuania OR Baltic) (policy OR security OR NATO OR language OR minority OR disinformation OR sanctions)'),
    ("Ukraine", '(Ukraine OR Ukrainian) (Estonia OR Latvia OR Lithuania OR Baltic OR NATO) (war OR security OR defence OR defense OR assistance OR sanctions OR energy)'),
    ("Regional actors", '("Baltic states" OR "Baltic region") (NATO OR EU OR Belarus OR Poland OR Nordic OR Sweden OR Finland OR infrastructure OR security)'),
    ("De-Russification", '(derussification OR "de-Russification" OR "Russian language" OR Russification) (Estonia OR Latvia OR Lithuania)'),
]


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"gemini_free_run_{datetime.now(TZ):%Y-%m-%d_%H-%M-%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def output_path(prefix: str, run_date: str, suffix: str) -> Path:
    path = INBOX_DIR / f"{prefix}_{run_date}.{suffix}"
    if not path.exists():
        return path
    stamp = datetime.now(TZ).strftime("%H-%M-%S")
    return INBOX_DIR / f"{prefix}_{run_date}_rerun_{stamp}.{suffix}"


def date_window() -> tuple[datetime, datetime, str, str]:
    today = datetime.now(TZ).date()
    start_day = today - timedelta(days=6)
    start_local = datetime.combine(start_day, dt_time.min, tzinfo=TZ)
    end_local = datetime.combine(today, dt_time.max, tzinfo=TZ)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    return start_utc, end_utc, start_day.isoformat(), today.isoformat()


def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
        # Remove fragments and common tracking query strings while preserving the original URL separately.
        return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", ""))
    except Exception:
        return url.strip()


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
            response = requests.get(GDELT_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=35)
            response.raise_for_status()
            payload = response.json()
            articles = payload.get("articles", [])
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
                logging.warning("GDELT sorgusu başarısız (%s): %s", category, exc)
                return []
            time.sleep(2 ** attempt)
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
        response = requests.get(candidate["url"], headers={"User-Agent": USER_AGENT}, timeout=18, allow_redirects=True)
        result["final_url"] = response.url
        result["http_status"] = response.status_code
        if response.status_code >= 400:
            result["page_access_status"] = f"HTTP {response.status_code}; content not inspected"
            return result
        content_type = response.headers.get("Content-Type", "")
        if "html" not in content_type.lower() and not response.text.lstrip().startswith("<"):
            result["page_access_status"] = f"Accessible, non-HTML content ({content_type or 'unknown type'})"
            return result
        soup = BeautifulSoup(response.text, "html.parser")
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
        text = re.sub(r"\s+", " ", article.get_text(" ", strip=True)).strip()
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


def build_model_prompt(template: str, candidates: list[dict[str, Any]], start_date: str, end_date: str, run_date: str) -> str:
    payload = []
    for item in candidates:
        payload.append({
            "title_from_gdelt": item.get("title"),
            "original_url": item.get("url"),
            "final_url_after_redirect": item.get("final_url", ""),
            "publisher_domain": item.get("domain"),
            "publisher_from_page": item.get("publisher_from_page", ""),
            "language": item.get("language"),
            "source_country": item.get("source_country"),
            "gdelt_seen_date_not_publication_date": item.get("gdelt_seen_date"),
            "discovery_category": item.get("discovery_category"),
            "page_access_status": item.get("page_access_status"),
            "page_title": item.get("page_title"),
            "publication_date_from_page": item.get("publication_date_from_page"),
            "page_text_excerpt": item.get("page_text_excerpt"),
        })
    source_blob = json.dumps(payload, ensure_ascii=False, indent=2)
    return (
        template.replace("{START_DATE}", start_date)
        .replace("{END_DATE}", end_date)
        .replace("{RUN_DATE}", run_date)
        .replace("{CANDIDATE_COUNT}", str(len(candidates)))
        + "\n\n## GDELT tarafından bulunan aday kayıtlar\n"
        + "Aşağıdaki JSON tek kaynak keşif havuzudur. JSON'da olmayan URL veya kaynak ekleme.\n\n"
        + source_blob
    )


def main() -> int:
    setup_logging()
    load_dotenv(ENV_FILE)
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or api_key == "PASTE_YOUR_API_KEY_HERE":
        logging.error("GEMINI_API_KEY bulunamadı. automation/.env dosyasını kontrol edin.")
        return 2
    if not PROMPT_FILE.exists():
        logging.error("Prompt dosyası bulunamadı: %s", PROMPT_FILE)
        return 2

    start_utc, end_utc, start_date, end_date = date_window()
    run_date = end_date
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    template = PROMPT_FILE.read_text(encoding="utf-8")
    model = os.getenv("GEMINI_MODEL", MODEL_DEFAULT).strip() or MODEL_DEFAULT

    logging.info("Ücretsiz kaynak keşif akışı başlatılıyor. Model: %s", model)
    logging.info("Araştırma dönemi: %s – %s (Türkiye takvim günleri)", start_date, end_date)
    logging.info("1/3: GDELT üzerinden çok dilli haber adayları aranıyor.")

    collected: dict[str, dict[str, Any]] = {}
    for category, query in SEARCHES:
        found = gdelt_search(query, start_utc, end_utc, category)
        logging.info("%s sorgusu: %d aday", category, len(found))
        for item in found:
            key = normalize_url(item["url"])
            if key and key not in collected:
                collected[key] = item
        time.sleep(0.8)

    candidates = list(collected.values())[:MAX_UNIQUE_CANDIDATES]
    if not candidates:
        logging.error("GDELT bu tarih aralığı ve sorgular için aday döndürmedi. Gemini çağrılmadı.")
        return 3
    logging.info("Toplam benzersiz aday: %d", len(candidates))

    logging.info("2/3: İlk %d aday sayfası otomatik olarak kontrol ediliyor.", min(MAX_PAGES_TO_INSPECT, len(candidates)))
    inspected: list[dict[str, Any]] = []
    for index, item in enumerate(candidates[:MAX_PAGES_TO_INSPECT], start=1):
        inspected.append(inspect_page(item))
        if index % 10 == 0:
            logging.info("Sayfa kontrolü: %d/%d", index, min(MAX_PAGES_TO_INSPECT, len(candidates)))
        time.sleep(0.2)
    inspected.extend(candidates[MAX_PAGES_TO_INSPECT:])

    candidates_file = output_path("gdelt_candidates", run_date, "json")
    candidates_file.write_text(json.dumps({
        "run_date": run_date,
        "research_window": {"start": start_date, "end": end_date, "inclusive": True},
        "discovery_source": "GDELT DOC 2.0 API",
        "note": "GDELT is a discovery index, not a comprehensive or authoritative source register. Page checks are automated and do not prove factual accuracy.",
        "candidates": inspected,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("Ham aday envanteri kaydedildi: %s", candidates_file)

    logging.info("3/3: Gemini Free Tier modeli adayları tez odağına göre sınıflandırıyor.")
    prompt = build_model_prompt(template, inspected, start_date, end_date, run_date)
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model, contents=prompt)
        report = (response.text or "").strip()
        if not report:
            raise RuntimeError("Gemini boş yanıt döndürdü.")
        report_file = output_path("gemini_raw", run_date, "md")
        header = (
            "# Gemini Free-Tier OSINT Source Discovery\n\n"
            f"- Çalıştırma tarihi: {run_date}\n"
            f"- Araştırma penceresi: {start_date} – {end_date} (dahil)\n"
            f"- Model: `{model}`\n"
            f"- Aday sayısı: {len(inspected)}\n"
            f"- Kaynak keşif altyapısı: GDELT DOC 2.0 API\n\n"
            "> **Yöntem sınırı:** Bu çıktı GDELT indeksindeki haber adaylarının otomatik keşfi ve Gemini ile ön sınıflandırılmasıdır. Deep Research değildir. GDELT tüm resmî belgeleri veya haberleri kapsamaz. Otomatik HTTP sayfa kontrolü, kaynağın iddialarının doğruluğunu kanıtlamaz. Antigravity her seçili orijinal kaynağı ayrıca açıp incelemelidir.\n\n---\n\n"
        )
        report_file.write_text(header + report + "\n", encoding="utf-8")
        logging.info("Gemini raporu kaydedildi: %s", report_file)
        logging.info("İşlem tamamlandı. Antigravity'nin bağımsız kaynak doğrulaması henüz yapılmadı.")
        return 0
    except Exception as exc:
        logging.exception("Gemini analiz çağrısı başarısız: %s", exc)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
