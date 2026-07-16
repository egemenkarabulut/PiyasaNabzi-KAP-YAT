#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
KAP YAT HAM + DETAY + TEFAS İŞLEM LİSTESİ DOĞRULAMA TESTİ v8
===================================

Bu dosya, önce KAP YF aktif fon ana listesini indirir; ardından seçilen fonların
KAP "Genel Bilgiler" sayfasına giderek yalnızca şu üç alanı test eder:

1) Başlangıç tarihi / başlangıç yılı
2) Risk seviyesi
3) TEFAS işlem durumu

Çalışma biçimi:
- KAP ana liste: JSON/API
- Fon detayları: KAP HTML sayfası
- Ayrıştırma: yalnızca görünür DOM metni ve tablo hücreleri
- Yerel fonpulse.db varsa karşılaştırma yapılır
- Veritabanına kesinlikle yazılmaz

Önemli:
- <script>, <style>, Next.js/React ham veri blokları ayrıştırmadan çıkarılır.
  Böylece başka tarihler yanlışlıkla "başlangıç yılı" olarak alınmaz.
- İşlem durumu KAP "Alım Satım Yerleri" alanı merkezli belirlenir. Alan boşsa,
  yalnızca kurucu/portföy yönetim şirketi/banka kanalları içeriyorsa veya açık
  olumsuz beyan varsa KAPALI kabul edilir. Çıplak TEFAS ifadesi, TEFAS'ın
  güncel "İşlem Gören Yatırım Fonları" listesiyle doğrulanır.

Kurulum:
    python -m pip install requests beautifulsoup4 lxml pypdf

Örnek kullanım:
    python kap_yat_ham_kaynak_testi.py
    python kap_yat_ham_kaynak_testi.py --codes PHE,TLY,AFO
    python kap_yat_ham_kaynak_testi.py --all
    python kap_yat_ham_kaynak_testi.py --all --limit 100
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from io import BytesIO
from urllib.parse import urljoin
from typing import Any, Iterable, Sequence

import requests
from bs4 import BeautifulSoup, Tag
from pypdf import PdfReader


# -----------------------------------------------------------------------------
# SABİTLER
# -----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "KAP_YAT_DETAY_TEST_CIKTILARI"
RAW_HTML_DIR = OUTPUT_DIR / "HAM_SAYFALAR"
RAW_DOCUMENT_DIR = OUTPUT_DIR / "HAM_BELGELER"

KAP_LIST_URL = "https://www.kap.org.tr/tr/api/fund/criteria/YF/Y"
KAP_DETAIL_BASE = "https://www.kap.org.tr/tr/fon-bilgileri/genel"
KAP_SUMMARY_BASE = "https://www.kap.org.tr/tr/fon-bilgileri/ozet"
TEFAS_TRADED_LIST_URL = "https://www.tefas.gov.tr/api/statistics/tefas/getFplFonList"

DEFAULT_CODES = [
    # Temel kaynak örnekleri
    "PHE", "TLY", "AFO", "AFT", "ALE",
    # Kullanıcı tarafından doğrulanan hata-daraltma grubu
    "ABG", "ABS", "AII", "AJL", "BRG", "ICF", "HTI",
    "DTZ", "GAH", "JET", "MBL", "TMC", "YSU",
]
DEFAULT_WORKERS = 2
DEFAULT_DELAY_SECONDS = 0.45
REQUEST_TIMEOUT_SECONDS = 50
MAX_RETRIES = 3
SCRIPT_VERSION = "v8-public-1"
SAVE_DIAGNOSTIC_FILES = os.getenv("KAP_SAVE_DIAGNOSTICS", "0") == "1"

HEADERS_JSON = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": "https://www.kap.org.tr/tr/YatirimFonlari/YF",
    "Cache-Control": "no-cache",
}

HEADERS_HTML = {
    "User-Agent": HEADERS_JSON["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": HEADERS_JSON["Accept-Language"],
    "Referer": "https://www.kap.org.tr/tr/YatirimFonlari/YF",
    "Cache-Control": "no-cache",
}

START_LABELS = (
    # Mevcut alternatiflerin tamamı korunur; yeni varyasyonlar yalnızca eklenir.
    "Fonun Halka Arz Tarihi",
    "Fon Halka Arz Tarihi",
    "Halka Arz Tarihi",
    "Fonun Halka Arza Başlama Tarihi",
    "Fonun Satış Başlangıç Tarihi",
    "Fon Paylarının Satış Başlangıç Tarihi",
    "Payların Satış Başlangıç Tarihi",
    "İlk Satış Tarihi",
    "Satış Başlangıç Tarihi",
    "Fonun Kuruluş Tarihi",
    "Kuruluş Tarihi",
    "Fonun Başlangıç Tarihi",
    "Başlangıç Tarihi",
    "Fonun İlk İhraç Tarihi",
    "İlk İhraç Tarihi",
    "İhraç Tarihi",
    "Public Offering Date of Fund",
    "Public Offering Date",
    "Sales Start Date of Fund",
    "Sales Start Date",
    "Inception Date",
    "Issue Date",
)

START_END_LABELS = (
    "Fonun Süresi",
    "Fonun Tasfiye Tarihi",
    "Temel Alım Satım Bilgileri",
    "Fund Duration",
)

RISK_SECTION_LABELS = (
    "Fonun Yatırım Stratejisi ve Risk Değeri",
    "Yatırım Stratejisi ve Risk Değeri",
    "Fonun Risk Değeri",
    "Risk Değeri ve Yatırım Stratejisi",
    "Risk Profili",
    "Investment Strategy and Risk Value",
    "Risk Profile",
)

RISK_LABELS = (
    "Risk Değeri",
    "Fonun Risk Değeri",
    "Risk Göstergesi",
    "Risk Seviyesi",
    "Risk Sınıfı",
    "Risk Grubu",
    "Risk Value",
    "Risk Indicator",
    "Risk Level",
    "Risk Class",
)

RISK_END_LABELS = (
    "Fon Karşılaştırma Ölçütü",
    "Karşılaştırma Ölçütü",
    "Eşik Değer",
    "Benchmark",
)

TRADE_SECTION_LABELS = (
    "Temel Alım Satım Bilgileri",
    "Alım Satım Bilgileri",
    "Alım Satım Yerleri",
    "Katılma Paylarının Alım ve Satımı",
    "Katılma Paylarının Alım Satım Esasları",
    "Katılma Paylarının Alım ve Satım Esasları",
    "Fon Paylarının Alım Satım Esasları",
    "Fon Paylarının Alım ve Satım Esasları",
    "Alım Satım Esasları",
    "İşlem Platformu",
    "TEFAS İşlem Durumu",
    "Main Trading Information",
    "Trading Information",
    "Trading Venues",
)

INVESTOR_FORM_LABELS = (
    "Yatırımcı Bilgi Formu",
    "Yatırımcı Bilgi Formu (YBF)",
    "Yatırımcı Bilgi Formu PDF",
    "YBF",
    "Investor Information Form",
    "Investor Information Form PDF",
)

TRADE_END_LABELS = (
    "Fon Portföyüne İlişkin Bilgiler",
    "Fonun Yatırım Stratejisi ve Risk Değeri",
    "Komisyon ve Gider Bilgileri",
    "Fund Portfolio Information",
)

TRADE_KEYWORDS = (
    "TEFAS",
    "TEFDP",
    "TEFAS'a",
    "TEFAS’a",
    "TEFAS'ta",
    "TEFAS’ta",
    "Türkiye Elektronik Fon Dağıtım Platformu",
    "Türkiye Elektronik Fon Alım Satım Platformu",
    "Türkiye Elektronik Fon Alım-Satım Platformu",
    "Elektronik Fon Dağıtım Platformu",
    "Elektronik Fon Alım Satım Platformu",
    "Elektronik Fon Alım-Satım Platformu",
    "Fon Dağıtım Platformu",
    "Fon Alım Satım Platformu",
)


# -----------------------------------------------------------------------------
# VERİ MODELLERİ
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class FundEntry:
    fund_code: str
    fund_name: str
    fund_oid: str
    fund_permalink: str
    fund_state: str
    fund_class: str

    @property
    def detail_url(self) -> str:
        if self.fund_permalink:
            return f"{KAP_DETAIL_BASE}/{self.fund_permalink}"
        return f"{KAP_DETAIL_BASE}/{self.fund_code.lower()}"

    @property
    def summary_url(self) -> str:
        if self.fund_permalink:
            return f"{KAP_SUMMARY_BASE}/{self.fund_permalink}"
        return f"{KAP_SUMMARY_BASE}/{self.fund_code.lower()}"


@dataclass
class ParseValue:
    value: str
    raw_value: str
    source_label: str
    evidence: str
    confidence: str
    matched_pattern: str = ""
    matched_scope: str = ""
    decision_reason: str = ""
    conflict_flag: str = "HAYIR"


@dataclass
class FundResult:
    test_time: str
    fund_code: str
    fund_name: str
    fund_oid: str
    fund_permalink: str
    detail_url: str
    final_url: str
    summary_url: str
    investor_form_url: str
    investor_form_http_status: int | None
    fallback_used: str
    fallback_error: str
    http_status: int | None
    response_ms: int | None
    page_code_verified: str

    start_date: str
    start_year: str
    start_source: str
    start_evidence: str
    start_confidence: str

    risk_level: str
    risk_detail: str
    risk_multi_value: str
    risk_source: str
    risk_evidence: str
    risk_confidence: str

    transaction_status: str
    transaction_source: str
    transaction_evidence: str
    transaction_confidence: str
    transaction_matched_pattern: str
    transaction_matched_scope: str
    transaction_decision_reason: str
    transaction_conflict_flag: str
    tefas_traded_list_match: str
    tefas_traded_list_status: str
    tefas_traded_list_title: str
    tefas_traded_list_date: str
    fallback_attempted: str
    fallback_winner: str

    db_start_year: str
    db_risk_level: str
    db_transaction_status: str
    compare_start: str
    compare_risk: str
    compare_transaction: str

    parse_method: str
    error: str


# -----------------------------------------------------------------------------
# METİN / TARİH YARDIMCILARI
# -----------------------------------------------------------------------------

def normalize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def fold_tr(value: Any) -> str:
    text = normalize_text(value).casefold()
    table = str.maketrans({
        "ı": "i", "İ": "i", "ş": "s", "Ş": "s",
        "ğ": "g", "Ğ": "g", "ç": "c", "Ç": "c",
        "ö": "o", "Ö": "o", "ü": "u", "Ü": "u",
    })
    text = text.translate(table)
    text = "".join(
        char for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )
    return re.sub(r"\s+", " ", text).strip()


def compact_for_match(value: Any) -> str:
    text = fold_tr(value)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def contains_any(value: Any, labels: Iterable[str]) -> bool:
    folded = fold_tr(value)
    return any(fold_tr(label) in folded for label in labels)


def truncate(value: Any, limit: int = 1000) -> str:
    text = normalize_text(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(0?[1-9]|[12]\d|3[01])\s*[./-]\s*(0?[1-9]|1[0-2])\s*[./-]\s*((?:19|20)\d{2})\b"),
    re.compile(r"\b((?:19|20)\d{2})\s*[./-]\s*(0?[1-9]|1[0-2])\s*[./-]\s*(0?[1-9]|[12]\d|3[01])\b"),
)


def find_dates(text: Any) -> list[date]:
    source = normalize_text(text)
    found: list[date] = []

    for match in DATE_PATTERNS[0].finditer(source):
        day, month, year = map(int, match.groups())
        try:
            found.append(date(year, month, day))
        except ValueError:
            pass

    for match in DATE_PATTERNS[1].finditer(source):
        year, month, day = map(int, match.groups())
        try:
            found.append(date(year, month, day))
        except ValueError:
            pass

    # Aynı tarih birden fazla yerde görünürse tekilleştir.
    return sorted(set(found))


def format_date_tr(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def normalize_status(value: Any) -> str:
    text = compact_for_match(value)
    if not text or text in {"—", "-"}:
        return ""
    if text in {"acik", "open", "aktif", "evet", "1"}:
        return "AÇIK"
    if text in {"kapali", "closed", "pasif", "hayir", "0"}:
        return "KAPALI"
    if "bilinmiyor" in text or "belirsiz" in text:
        return "BİLİNMİYOR"
    return normalize_text(value).upper()


def compare_values(kap_value: str, db_value: str, normalizer=None) -> str:
    kap = normalizer(kap_value) if normalizer else normalize_text(kap_value)
    db = normalizer(db_value) if normalizer else normalize_text(db_value)

    kap_empty = not kap or kap in {"—", "-", "BİLİNMİYOR"}
    db_empty = not db or db in {"—", "-", "BİLİNMİYOR"}

    if kap_empty and db_empty:
        return "İKİSİ DE BOŞ"
    if kap_empty:
        return "KAP BOŞ"
    if db_empty:
        return "DB BOŞ"
    return "EŞLEŞTİ" if kap == db else "FARKLI"


# -----------------------------------------------------------------------------
# İSTEK KATMANI
# -----------------------------------------------------------------------------

class GlobalRateLimiter:
    """Tüm thread'ler için ortak minimum istek aralığı uygular."""

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval = max(0.0, float(min_interval_seconds))
        self._lock = threading.Lock()
        self._last_request = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait_seconds = self.min_interval - (now - self._last_request)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            self._last_request = time.monotonic()


_thread_local = threading.local()


def thread_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS_HTML)
        _thread_local.session = session
    return session


def request_with_retry(
    session: requests.Session,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    rate_limiter: GlobalRateLimiter | None = None,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> tuple[requests.Response, int]:
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        if rate_limiter is not None:
            rate_limiter.wait()
        started = time.perf_counter()
        try:
            response = session.get(
                url,
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)

            if response.status_code == 429:
                retry_after = normalize_text(response.headers.get("Retry-After"))
                wait_seconds = float(retry_after) if retry_after.isdigit() else attempt * 4.0
                time.sleep(wait_seconds)
                continue

            if response.status_code >= 500 and attempt < MAX_RETRIES:
                time.sleep(attempt * 2.0)
                continue

            response.raise_for_status()
            return response, elapsed_ms
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 2.0)

    raise RuntimeError(f"İstek başarısız: {url} | {type(last_error).__name__}: {last_error}")



def post_json_with_retry(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> tuple[requests.Response, int]:
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        started = time.perf_counter()
        try:
            response = session.post(
                url,
                json=payload,
                headers=headers or HEADERS_JSON,
                timeout=timeout,
                allow_redirects=True,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if response.status_code == 429:
                retry_after = normalize_text(response.headers.get("Retry-After"))
                wait_seconds = float(retry_after) if retry_after.isdigit() else attempt * 4.0
                time.sleep(wait_seconds)
                continue
            if response.status_code >= 500 and attempt < MAX_RETRIES:
                time.sleep(attempt * 2.0)
                continue
            response.raise_for_status()
            return response, elapsed_ms
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 2.0)
    raise RuntimeError(
        f"POST isteği başarısız: {url} | {type(last_error).__name__}: {last_error}"
    )


def fetch_tefas_traded_funds() -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    """TEFAS 'İşlem Gören Yatırım Fonları' listesini tek çağrıda alır.

    Bu liste fiyat/NAV yayımlanan tüm fonların listesi değildir. TEFAS istatistik
    servisindeki güncel işlem gören fon listesidir ve özellikle KAP alanında
    yalnızca çıplak 'TEFAS' ibaresi bulunan kayıtları doğrulamak için kullanılır.
    """
    session = requests.Session()
    session.headers.update(HEADERS_JSON)
    response, _ = post_json_with_retry(
        session,
        TEFAS_TRADED_LIST_URL,
        {},
        headers=HEADERS_JSON,
        timeout=60,
    )
    payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("TEFAS işlem gören fon listesi cevabında 'data' listesi yok.")

    result: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = normalize_text(row.get("fonKod")).upper()
        if not code:
            continue
        result[code] = {
            "fonKod": code,
            "unvan": normalize_text(row.get("unvan")),
            "kurucuKod": normalize_text(row.get("kurucuKod")),
            "kurucuAd": normalize_text(row.get("kurucuAd")),
            "oprKod": normalize_text(row.get("oprKod")),
            "oprAd": normalize_text(row.get("oprAd")),
            "durum": normalize_text(row.get("durum")),
            "tarih": normalize_text(row.get("tarih")),
        }
    return result, payload


def fetch_kap_fund_list() -> tuple[list[FundEntry], list[dict[str, Any]]]:
    session = requests.Session()
    response, _ = request_with_retry(
        session,
        KAP_LIST_URL,
        headers=HEADERS_JSON,
        timeout=60,
    )
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"KAP ana liste cevabı liste değil: {type(payload).__name__}")

    funds: list[FundEntry] = []
    raw_rows: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        raw_rows.append(row)
        code = normalize_text(row.get("fundCode")).upper()
        if not code:
            continue
        funds.append(FundEntry(
            fund_code=code,
            fund_name=normalize_text(row.get("fundName")),
            fund_oid=normalize_text(row.get("fundOid")),
            fund_permalink=normalize_text(row.get("fundPermaLink")),
            fund_state=normalize_text(row.get("fundState")),
            fund_class=normalize_text(row.get("fundClass")),
        ))

    # Aynı kod tekrar ederse ilk kaydı koru.
    unique: dict[str, FundEntry] = {}
    for fund in funds:
        unique.setdefault(fund.fund_code, fund)
    return sorted(unique.values(), key=lambda item: item.fund_code), raw_rows


# -----------------------------------------------------------------------------
# HTML AYRIŞTIRMA
# -----------------------------------------------------------------------------

def clean_soup(html: str) -> BeautifulSoup:
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # Next.js/React payloadları ve görünmeyen içerikler yanlış tarih/risk üretebilir.
    for tag in soup.find_all(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    return soup


def visible_lines(soup: BeautifulSoup) -> list[str]:
    lines: list[str] = []
    for item in soup.stripped_strings:
        text = normalize_text(item)
        if text:
            lines.append(text)
    return lines


def table_rows(soup: BeautifulSoup) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in soup.find_all("tr"):
        cells = [
            normalize_text(cell.get_text(" ", strip=True))
            for cell in tr.find_all(["th", "td"])
        ]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(cells)
    return rows



def table_rows_with_empty(soup: BeautifulSoup) -> list[list[str]]:
    """Tablo hücrelerini boş değerleri koruyarak döndürür."""
    rows: list[list[str]] = []
    for tr in soup.find_all("tr"):
        cells = [
            normalize_text(cell.get_text(" ", strip=True))
            for cell in tr.find_all(["th", "td"])
        ]
        if cells:
            rows.append(cells)
    return rows


def extract_trade_place_value(soup: BeautifulSoup) -> tuple[bool, str, str]:
    """Alım Satım Yerleri alanının mevcut olup olmadığını ve değerini çıkarır.

    Boş hücreler özellikle korunur. Böylece alanın sayfada bulunmaması ile alanın
    bulunup değerinin boş olması birbirinden ayrılır.
    """
    target_variants = (
        "Alım Satım Yerleri",
        "Alım-Satım Yerleri",
        "Alım ve Satım Yerleri",
        "Trading Venues",
    )
    target_folds = tuple(fold_tr(item) for item in target_variants)
    header_labels = tuple(fold_tr(item) for item in (
        "Alım Satım Saatleri",
        "Alım Satım Yerleri",
        "Alınabilecek Asgari Pay Adedi",
        "Nemalandırma Esasları",
        "Diğer Önemli Bilgiler",
    ))

    for table in soup.find_all("table"):
        rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            cells = [
                normalize_text(cell.get_text(" ", strip=True))
                for cell in tr.find_all(["th", "td"])
            ]
            if cells:
                rows.append(cells)

        for row_index, row in enumerate(rows):
            target_indexes = [
                idx for idx, cell in enumerate(row)
                if any(label in fold_tr(cell) for label in target_folds)
            ]
            if not target_indexes:
                continue

            target_index = target_indexes[0]
            for candidate in rows[row_index + 1: row_index + 7]:
                candidate_folded = [fold_tr(cell) for cell in candidate]
                if any(
                    any(label in cell for label in header_labels)
                    for cell in candidate_folded
                ):
                    continue
                # Tek hücreli bölüm başlıklarını atla.
                if len(candidate) == 1 and contains_any(
                    candidate[0], ("Temel Alım Satım Bilgileri", "Trading Information")
                ):
                    continue

                offsets = []
                length_offset = len(candidate) - len(row)
                offsets.extend([length_offset, 0, -1, 1, -2, 2])
                seen: set[int] = set()
                for offset in offsets:
                    idx = target_index + offset
                    if idx in seen or idx < 0 or idx >= len(candidate):
                        continue
                    seen.add(idx)
                    # Veri satırı olma olasılığı: en az iki sütun ya da diğer
                    # hücrelerde anlamlı veri bulunmalı. Hedef hücre boş olabilir.
                    other_nonempty = sum(
                        1 for pos, value in enumerate(candidate)
                        if pos != idx and normalize_text(value)
                    )
                    if len(candidate) >= 2 and other_nonempty >= 1:
                        value = normalize_text(candidate[idx])
                        evidence = " | ".join(candidate)
                        return True, value, evidence

            # Etiket ve değer aynı satırda iki ayrı hücre olabilir.
            if len(row) > target_index + 1:
                value = normalize_text(row[target_index + 1])
                return True, value, " | ".join(row)

    # Genel DOM yedeği: etiket düğümünün aynı satırındaki / sonraki hücresindeki değer.
    for node in soup.find_all(string=True):
        own = normalize_text(node)
        if not own or not any(label in fold_tr(own) for label in target_folds):
            continue
        parent = node.parent if isinstance(node.parent, Tag) else None
        if parent is None:
            continue
        cell = parent.find_parent(["th", "td"])
        if cell is not None:
            sibling = cell.find_next_sibling(["th", "td"])
            if sibling is not None:
                value = normalize_text(sibling.get_text(" ", strip=True))
                return True, value, normalize_text(
                    cell.get_text(" ", strip=True) + " | " + sibling.get_text(" ", strip=True)
                )

    return False, "", ""


def find_section_lines(
    lines: Sequence[str],
    start_labels: Sequence[str],
    end_labels: Sequence[str],
    *,
    max_lines: int = 50,
) -> list[str]:
    start_index = -1
    for index, line in enumerate(lines):
        if contains_any(line, start_labels):
            start_index = index
            break
    if start_index < 0:
        return []

    section = [lines[start_index]]
    for line in lines[start_index + 1: start_index + max_lines]:
        if contains_any(line, end_labels):
            break
        section.append(line)
    return section


def nearby_dom_text(
    soup: BeautifulSoup,
    labels: Sequence[str],
    *,
    max_chars: int = 2500,
) -> list[str]:
    """Etiket çevresindeki küçük DOM bloklarını yedek kanıt olarak toplar."""
    found: list[str] = []
    folded_labels = [fold_tr(label) for label in labels]

    for node in soup.find_all(string=True):
        own = normalize_text(node)
        if not own:
            continue
        own_folded = fold_tr(own)
        if not any(label in own_folded for label in folded_labels):
            continue

        parent = node.parent if isinstance(node.parent, Tag) else None
        candidates: list[Tag] = []
        if parent is not None:
            candidates.append(parent)
            if isinstance(parent.parent, Tag):
                candidates.append(parent.parent)
            if isinstance(parent.parent, Tag) and isinstance(parent.parent.parent, Tag):
                candidates.append(parent.parent.parent)

        for tag in candidates:
            text = normalize_text(tag.get_text(" | ", strip=True))
            if own in text and 0 < len(text) <= max_chars and text not in found:
                found.append(text)
    return found


def _dates_near_label_in_rows(
    soup: BeautifulSoup,
    label: str,
) -> tuple[list[date], str]:
    folded_label = fold_tr(label)
    rows = table_rows_with_empty(soup)
    for index, row in enumerate(rows):
        for cell_index, cell in enumerate(row):
            if folded_label not in fold_tr(cell):
                continue
            # Önce yalnızca etiketin bulunduğu satırı değerlendir. Aynı satırda
            # tarih varken sonraki satırdaki bildirim/onay tarihi karara karışmaz.
            same_row_pieces = [cell]
            if cell_index + 1 < len(row):
                same_row_pieces.extend(row[cell_index + 1:])
            evidence = " | ".join(
                normalize_text(item) for item in same_row_pieces if normalize_text(item)
            )
            dates = find_dates(evidence)
            if dates:
                return dates, evidence

            # Dikey tabloda değer bir sonraki satırda olabilir; bu yedek yalnızca
            # etiket satırında hiç tarih bulunmadığında çalışır.
            if index + 1 < len(rows):
                next_row = rows[index + 1]
                if len(next_row) <= max(3, len(row) + 1):
                    next_evidence = " | ".join(
                        normalize_text(item) for item in next_row[:3] if normalize_text(item)
                    )
                    dates = find_dates(next_evidence)
                    if dates:
                        return dates, normalize_text(evidence + " | " + next_evidence)
    return [], ""


def _dates_near_label_in_lines(lines: Sequence[str], label: str) -> tuple[list[date], str]:
    folded_label = fold_tr(label)
    for index, line in enumerate(lines):
        if folded_label not in fold_tr(line):
            continue
        # Etiketle aynı görünür satırda tarih varsa doğrudan kullan.
        dates = find_dates(line)
        if dates:
            return dates, line

        window = [line]
        for next_line in lines[index + 1:index + 3]:
            # Başka bir başlangıç etiketi başladıysa pencereyi bitir.
            if contains_any(next_line, START_LABELS) and folded_label not in fold_tr(next_line):
                break
            window.append(next_line)
            next_dates = find_dates(next_line)
            if next_dates:
                return next_dates, " | ".join(window)
        evidence = " | ".join(window)
    return [], ""


def extract_start(soup: BeautifulSoup, lines: Sequence[str]) -> ParseValue:
    """Başlangıç tarihini etiket önceliğine göre seçer.

    Aynı bölümde geçen bildirim/onay/tescil gibi ilgisiz eski tarihler artık
    minimum tarih seçilerek başlangıç kabul edilmez.
    """
    for label in START_LABELS:
        dates, evidence = _dates_near_label_in_rows(soup, label)
        if not dates:
            dates, evidence = _dates_near_label_in_lines(lines, label)
        if not dates:
            for block in nearby_dom_text(soup, (label,), max_chars=1200):
                dates = find_dates(block)
                if dates:
                    evidence = block
                    break
        if dates:
            selected = dates[0]
            return ParseValue(
                value=str(selected.year),
                raw_value=format_date_tr(selected),
                source_label=f"KAP_DETAIL_HTML:{label}",
                evidence=truncate(evidence, 1200),
                confidence="YÜKSEK",
                decision_reason="Başlangıç etiketi öncelik sırasına göre seçildi; ilgisiz en eski tarih kullanılmadı.",
            )

    return ParseValue(
        value="—",
        raw_value="",
        source_label="KAP_DETAIL_HTML:Fonun Halka Arz Tarihi",
        evidence="İlgili görünür KAP alanlarında geçerli başlangıç tarihi bulunamadı.",
        confidence="YOK",
    )


def _risk_values_from_text(value: str) -> tuple[list[int], str]:
    compact = compact_for_match(value)
    values: list[int] = []
    details: list[str] = []

    contextual_patterns = (
        r"(?:usd|eur|gbp|chf|tl|try|a grubu|b grubu|c grubu|d grubu|pay grubu).{0,45}\b([1-7])\b",
        r"\b([1-7])\b.{0,35}(?:usd|eur|gbp|chf|tl|try|a grubu|b grubu|c grubu|d grubu|pay grubu)",
        r"(?:risk degeri|risk seviyesi|risk gostergesi|risk sinifi|risk grubu).{0,80}\b([1-7])\b",
        r"\b([1-7])\s*/\s*7\b",
    )
    for pattern in contextual_patterns:
        for match in re.finditer(pattern, compact):
            number = int(match.group(1))
            values.append(number)
            details.append(match.group(0))

    # Hücre yalnızca bir risk rakamıysa en güvenilir sade biçim.
    clean = normalize_text(value)
    if re.fullmatch(r"[1-7]", clean):
        values.append(int(clean))
        details.append(clean)

    unique = sorted(set(values))
    return unique, " | ".join(dict.fromkeys(details))


def extract_risk(soup: BeautifulSoup, lines: Sequence[str]) -> ParseValue:
    evidence_parts: list[str] = []
    all_values: list[int] = []
    detail_parts: list[str] = []

    rows = table_rows_with_empty(soup)
    for index, row in enumerate(rows):
        joined = " | ".join(row)
        if not contains_any(joined, RISK_LABELS):
            continue
        window = rows[index:index + 6]
        evidence = " || ".join(" | ".join(item) for item in window)
        evidence_parts.append(evidence)
        values, detail = _risk_values_from_text(evidence)
        all_values.extend(values)
        if detail:
            detail_parts.append(detail)
        if values:
            break

    if not all_values:
        section = find_section_lines(lines, RISK_SECTION_LABELS, RISK_END_LABELS, max_lines=50)
        if not section:
            section = find_section_lines(lines, RISK_LABELS, RISK_END_LABELS, max_lines=30)
        if section:
            evidence = " | ".join(section)
            evidence_parts.append(evidence)
            values, detail = _risk_values_from_text(evidence)
            all_values.extend(values)
            if detail:
                detail_parts.append(detail)

    if not all_values:
        for block in nearby_dom_text(soup, RISK_LABELS, max_chars=1800):
            evidence_parts.append(block)
            values, detail = _risk_values_from_text(block)
            all_values.extend(values)
            if detail:
                detail_parts.append(detail)
            if values:
                break

    values = sorted(set(v for v in all_values if 1 <= v <= 7))
    if not values:
        return ParseValue(
            value="—",
            raw_value="",
            source_label="KAP_DETAIL_HTML:Risk Değeri",
            evidence=truncate(" || ".join(evidence_parts), 1400)
            or "İlgili görünür KAP bölümünde güvenilir 1-7 risk değeri bulunamadı.",
            confidence="YOK",
        )

    selected = max(values)
    detail = ", ".join(str(item) for item in values)
    return ParseValue(
        value=str(selected),
        raw_value=detail,
        source_label="KAP_DETAIL_HTML:Risk Değeri",
        evidence=truncate(" || ".join(evidence_parts), 1400),
        confidence="YÜKSEK" if len(values) == 1 else "ÇOK YÜKSEK",
        decision_reason=(
            "Tek risk değeri bulundu."
            if len(values) == 1
            else f"Birden fazla pay grubu risk değeri bulundu ({detail}); ana risk olarak en yüksek değer {selected} seçildi."
        ),
    )


def extract_transaction(
    soup: BeautifulSoup,
    lines: Sequence[str],
    *,
    fund_code: str,
    tefas_traded_funds: dict[str, dict[str, str]],
    tefas_list_error: str = "",
) -> ParseValue:
    """KAP Alım Satım Yerleri odaklı TEFAS işlem durumu kararı.

    Nihai sıra:
    1. Açık olumsuz beyan -> KAPALI
    2. Açık TEFAS/TEFDP üyelik-dağıtım beyanı -> AÇIK
    3. Alım Satım Yerleri boş/Bilgi Mevcut Değil -> KAPALI
    4. Sadece kurucu/portföy yöneticisi/banka kanalları -> KAPALI
    5. Yalnız çıplak TEFAS ibaresi -> güncel TEFAS işlem gören fon listesi
    6. Başka kesin AÇIK kanıt yoksa -> KAPALI
    7. Teknik erişim/ayrıştırma hatası -> BİLİNMİYOR
    """
    field_found, field_value, field_evidence = extract_trade_place_value(soup)
    field_compact = compact_for_match(field_value)

    section = find_section_lines(lines, TRADE_SECTION_LABELS, TRADE_END_LABELS, max_lines=80)
    section_text = " | ".join(section)
    section_compact = compact_for_match(section_text)

    # Dar tablo satırları ve bölüm metni birlikte yalnızca açık beyan tespiti için tutulur.
    scopes: list[tuple[str, str, str]] = []
    if field_found:
        scopes.append(("KAP_DETAIL_HTML:Alım Satım Yerleri", "ALIM_SATIM_YERI_DEGERI", field_value))
    if section_text:
        scopes.append(("KAP_DETAIL_HTML:Temel Alım Satım Bilgileri", "ALIM_SATIM_BÖLÜMÜ", section_text))

    # Sayfada açık olumsuz beyan, tüm olumlu genel kelimelerden üstündür.
    negative_patterns = (
        r"tefas.{0,100}islem gormemektedir",
        r"tefas.{0,100}islem gormez",
        r"tefas.{0,100}islem yapilmaz",
        r"tefas.{0,100}alim satima konu degildir",
        r"tefas.{0,100}acik degildir",
        r"tefas.{0,100}kapalidir",
        r"tefas.{0,100}dahil degildir",
        r"tefas.{0,100}yer almamaktadir",
        r"tefasa kapali",
        r"tefas disi",
        r"turkiye elektronik fon (?:alim satim|dagitim) platformu.{0,130}islem gormemektedir",
        r"turkiye elektronik fon (?:alim satim|dagitim) platformu.{0,130}islem gormez",
        r"turkiye elektronik fon (?:alim satim|dagitim) platformu.{0,130}dahil degildir",
        r"platformda.{0,80}islem gormemektedir",
        r"platformda.{0,80}islem gormez",
        r"katilma paylarinin alim ve satimi.{0,120}(?:yalnizca|sadece) kurucu",
        r"fon paylarinin alim ve satimi.{0,120}(?:yalnizca|sadece) kurucu",
        r"katilma paylarinin alim ve satimi.{0,80}kurucu araciligiyla yapilir.{0,160}"
        r"(?:tefas|turkiye elektronik fon alim satim platformu).{0,100}islem gormemektedir",
    )
    for source_label, scope_name, scope_text in scopes:
        compact = compact_for_match(scope_text)
        for pattern in negative_patterns:
            match = re.search(pattern, compact)
            if match:
                return ParseValue(
                    value="KAPALI",
                    raw_value="TEFAS için açık olumsuz beyan",
                    source_label=source_label,
                    evidence=truncate(match.group(0), 1400),
                    confidence="ÇOK YÜKSEK",
                    matched_pattern=pattern,
                    matched_scope=scope_name,
                    decision_reason="Açık TEFAS/platform olumsuz beyanı bulundu.",
                )

    # Kullanıcının kesin AÇIK kabul ettiği doğrudan cümleler ve TEFDP varyasyonları.
    direct_open_patterns = (
        r"katilma paylarinin alim ve satimi.{0,55}kurucunun yani sira.{0,130}"
        r"tefas(?: a)? uye olan fon dagitim kuruluslari araciligiyla(?: da)? "
        r"(?:yapilir|yapilmaktadir|gerceklestirilir|gerceklestirilmektedir)",
        r"katilma paylarinin alim ve satimi.{0,55}kurucunun yani sira.{0,150}"
        r"turkiye elektronik fon (?:alim satim|dagitim) platformu(?:na| na)? "
        r"uye olan fon dagitim kuruluslari araciligiyla(?: da)? "
        r"(?:yapilir|yapilmaktadir|gerceklestirilir|gerceklestirilmektedir)",
        r"kurucunun yani sira.{0,180}(?:tefas(?: a)?|tefdp(?: ye|ye)?|turkiye elektronik fon "
        r"(?:alim satim|dagitim) platformu(?:na| na)?) uye olan fon dagitim "
        r"kuruluslari araciligiyla(?: da)? (?:yapilir|yapilmaktadir|gerceklestirilir|gerceklestirilmektedir)",
        r"kurucu ve tefdp(?: ye|ye)? uye olan fon dagitim kuruluslari",
        r"tefdp(?: ye|ye)? uye olan fon dagitim kuruluslari",
        r"tefas(?: a)? uye olan fon dagitim kuruluslari",
        r"tefas uyesi fon dagitim kuruluslari",
    )
    for source_label, scope_name, scope_text in scopes:
        # Alım Satım Yerleri alanı mevcutsa geniş bölüm metni bu alanın
        # boş/kurucu/banka kararını geçersiz kılamaz. Doğrudan AÇIK cümle
        # yalnızca alan değerinin kendi içinde aranır. Alan çıkarılamadıysa
        # bölüm metni yedek olarak kullanılabilir.
        if field_found and scope_name != "ALIM_SATIM_YERI_DEGERI":
            continue
        compact = compact_for_match(scope_text)
        for pattern in direct_open_patterns:
            match = re.search(pattern, compact)
            if match:
                return ParseValue(
                    value="AÇIK",
                    raw_value="Kurucu yanında TEFAS/TEFDP üyesi fon dağıtım kuruluşları",
                    source_label=source_label,
                    evidence=truncate(match.group(0), 1400),
                    confidence="ÇOK YÜKSEK",
                    matched_pattern=pattern,
                    matched_scope=scope_name,
                    decision_reason="Doğrudan üyelik/dağıtım cümlesi TEFAS erişimini açıkça kanıtlıyor.",
                )

    # Alan ayrı hücre olarak çıkarılamasa bile ilgili Temel Alım Satım
    # bölümünde platformun tam adı veya TEFDP doğrudan yazıyorsa AÇIK kabul edilir.
    # Bu özellikle KAP DOM yapısının değiştiği AFO benzeri sayfalara karşı yedektir.
    if not field_found and section_compact:
        section_platform_patterns = (
            r"turkiye elektronik fon dagitim platformu",
            r"turkiye elektronik fon alim satim platformu",
            r"elektronik fon dagitim platformu",
            r"elektronik fon alim satim platformu",
            r"\btefdp\b",
        )
        for pattern in section_platform_patterns:
            match = re.search(pattern, section_compact)
            if match:
                return ParseValue(
                    value="AÇIK",
                    raw_value="Alım Satım bölümünde platformun tam adı/TEFDP",
                    source_label="KAP_DETAIL_HTML:Temel Alım Satım Bilgileri",
                    evidence=truncate(section_text, 1600),
                    confidence="YÜKSEK",
                    matched_pattern=pattern,
                    matched_scope="ALIM_SATIM_BÖLÜMÜ",
                    decision_reason="Alım Satım Yerleri hücresi ayrı çıkarılamadı; ilgili bölüm platformu doğrudan belirttiği için AÇIK kabul edildi.",
                )

    # Alım Satım Yerleri alanı sayfada bulunmuşsa kullanıcının alan-merkezli
    # kesin kuralları uygulanır.
    if field_found:
        empty_tokens = {
            "", "-", "—", "bilgi mevcut degil", "bilgi bulunmamaktadir",
            "bilgi yok", "mevcut degil", "yok", "belirtilmemistir",
        }
        if field_compact in empty_tokens:
            return ParseValue(
                value="KAPALI",
                raw_value="Alım Satım Yerleri alanı boş",
                source_label="KAP_DETAIL_HTML:Alım Satım Yerleri",
                evidence=truncate(field_evidence or "Alım Satım Yerleri: boş", 1200),
                confidence="ÇOK YÜKSEK",
                matched_pattern="ALIM_SATIM_YERI_BOS",
                matched_scope="ALIM_SATIM_YERI_DEGERI",
                decision_reason="Alım Satım Yerleri satırı boş/Bilgi Mevcut Değil olduğu için KAPALI kabul edildi.",
            )

        only_founder_patterns = (
            r"(?:sadece|yalnizca) kurucu bunyesinde",
            r"(?:sadece|yalnizca) kurucu(?:nun)? araciligiyla",
            r"(?:sadece|yalnizca) kurucu nezdinde",
            r"sadece kurucu",
            r"yalnizca kurucu",
        )
        for pattern in only_founder_patterns:
            match = re.search(pattern, field_compact)
            if match:
                return ParseValue(
                    value="KAPALI",
                    raw_value="Sadece Kurucu bünyesinde",
                    source_label="KAP_DETAIL_HTML:Alım Satım Yerleri",
                    evidence=truncate(field_value, 1200),
                    confidence="ÇOK YÜKSEK",
                    matched_pattern=pattern,
                    matched_scope="ALIM_SATIM_YERI_DEGERI",
                    decision_reason="Alım Satım Yerleri alanı işlemi yalnızca kurucu ile sınırlandırıyor.",
                )

        # Tam platform adı doğrudan alan değerindeyse kesin AÇIK.
        full_platform_patterns = (
            r"turkiye elektronik fon dagitim platformu",
            r"turkiye elektronik fon alim satim platformu",
            r"elektronik fon dagitim platformu",
            r"elektronik fon alim satim platformu",
            r"\btefdp\b",
        )
        for pattern in full_platform_patterns:
            match = re.search(pattern, field_compact)
            if match:
                return ParseValue(
                    value="AÇIK",
                    raw_value="Alım Satım Yerleri alanında platform adı/TEFDP",
                    source_label="KAP_DETAIL_HTML:Alım Satım Yerleri",
                    evidence=truncate(field_value, 1200),
                    confidence="ÇOK YÜKSEK",
                    matched_pattern=pattern,
                    matched_scope="ALIM_SATIM_YERI_DEGERI",
                    decision_reason="Alım Satım Yerleri alanı platformu doğrudan belirtiyor.",
                )

        # Çıplak TEFAS ifadesi tek başına yeterli değildir. TEFAS'ın güncel
        # işlem gören yatırım fonları listesiyle doğrulanır (ICF/JET ayrımı).
        if re.search(r"\btefas\b", field_compact):
            if tefas_list_error:
                return ParseValue(
                    value="BİLİNMİYOR",
                    raw_value="Çıplak TEFAS ifadesi; ikinci kaynak erişilemedi",
                    source_label="KAP_DETAIL_HTML + TEFAS_TRADED_LIST",
                    evidence=truncate(field_value, 1200),
                    confidence="YOK",
                    matched_pattern=r"\btefas\b",
                    matched_scope="ALIM_SATIM_YERI_DEGERI",
                    decision_reason=f"TEFAS işlem gören fon listesi teknik hatası: {tefas_list_error}",
                )
            if fund_code.upper() in tefas_traded_funds:
                return ParseValue(
                    value="AÇIK",
                    raw_value="Çıplak TEFAS + güncel TEFAS işlem listesinde kod mevcut",
                    source_label="KAP_DETAIL_HTML + TEFAS:getFplFonList",
                    evidence=truncate(field_value, 1200),
                    confidence="ÇOK YÜKSEK",
                    matched_pattern=r"\btefas\b + TEFAS_LIST_MATCH",
                    matched_scope="ALIM_SATIM_YERI_DEGERI",
                    decision_reason="KAP alanındaki çıplak TEFAS ibaresi, güncel TEFAS işlem gören fon listesiyle doğrulandı.",
                )
            return ParseValue(
                value="KAPALI",
                raw_value="Çıplak TEFAS + güncel TEFAS işlem listesinde kod yok",
                source_label="KAP_DETAIL_HTML + TEFAS:getFplFonList",
                evidence=truncate(field_value, 1200),
                confidence="ÇOK YÜKSEK",
                matched_pattern=r"\btefas\b + TEFAS_LIST_NO_MATCH",
                matched_scope="ALIM_SATIM_YERI_DEGERI",
                decision_reason="KAP alanında TEFAS kelimesi var; ancak kod güncel TEFAS işlem gören fon listesinde bulunmadığı için KAPALI.",
            )

        # Alan yalnızca portföy yönetim şirketi, kurucu, banka, şube,
        # yatırım kuruluşu veya alternatif banka kanallarını içeriyorsa KAPALI.
        institution_markers = (
            "portfoy yonetimi", "kurucu", "banka", "bankasi", "bank ",
            "sube", "subeleri", "yatirim menkul", "menkul degerler",
            "alternatif dagitim kanallari", "internet subesi", "mobil sube",
            "telefon bankaciligi", "ticaret a s", "a s",
        )
        if any(marker in f" {field_compact} " for marker in institution_markers):
            return ParseValue(
                value="KAPALI",
                raw_value="Yalnızca kurucu/portföy yöneticisi/banka kanalları",
                source_label="KAP_DETAIL_HTML:Alım Satım Yerleri",
                evidence=truncate(field_value, 1200),
                confidence="ÇOK YÜKSEK",
                matched_pattern="SADECE_KURUCU_PORTFOY_BANKA_KANALLARI",
                matched_scope="ALIM_SATIM_YERI_DEGERI",
                decision_reason="Alım Satım Yerleri alanında yalnızca kurucu/portföy yöneticisi/banka veya bağlı dağıtım kanalları bulundu; TEFAS KAPALI kabul edildi.",
            )

        # Alanda anlamlı fakat tanınmayan bir kanal varsa ve açık TEFAS kanıtı
        # yoksa kullanıcının 'aksi önemli beyan yoksa KAPALI' kuralı uygulanır.
        return ParseValue(
            value="KAPALI",
            raw_value="Alım Satım Yerleri alanında açık TEFAS kanıtı yok",
            source_label="KAP_DETAIL_HTML:Alım Satım Yerleri",
            evidence=truncate(field_value, 1200),
            confidence="YÜKSEK",
            matched_pattern="ALIM_SATIM_YERI_TEFAS_KANITI_YOK",
            matched_scope="ALIM_SATIM_YERI_DEGERI",
            decision_reason="Alım Satım Yerleri alanında kesin AÇIK beyan bulunmadığı için KAPALI kabul edildi.",
        )

    # Alan DOM'da hiç çıkarılamadıysa bölümdeki açık cümleleri kullan.
    # Çıplak TEFAS yine ikinci kaynakla doğrulanır.
    if section_compact:
        if re.search(r"\btefas\b", section_compact):
            # TEFAS yalnızca yakın satır/sözlükte olabilir; liste doğrulaması şart.
            if tefas_list_error:
                return ParseValue(
                    value="BİLİNMİYOR",
                    raw_value="TEFAS ifadesi var; alan çıkarılamadı ve liste erişilemedi",
                    source_label="KAP_DETAIL_HTML + TEFAS_TRADED_LIST",
                    evidence=truncate(section_text, 1600),
                    confidence="YOK",
                    matched_pattern=r"\btefas\b",
                    matched_scope="ALIM_SATIM_BÖLÜMÜ",
                    decision_reason=f"TEFAS işlem gören fon listesi teknik hatası: {tefas_list_error}",
                )
            if fund_code.upper() in tefas_traded_funds:
                return ParseValue(
                    value="AÇIK",
                    raw_value="Bölümde TEFAS + güncel listede kod mevcut",
                    source_label="KAP_DETAIL_HTML + TEFAS:getFplFonList",
                    evidence=truncate(section_text, 1600),
                    confidence="YÜKSEK",
                    matched_pattern=r"\btefas\b + TEFAS_LIST_MATCH",
                    matched_scope="ALIM_SATIM_BÖLÜMÜ",
                    decision_reason="Alım Satım Yerleri hücresi ayrı çıkarılamadı; bölümdeki TEFAS ibaresi güncel TEFAS listesiyle doğrulandı.",
                )
            return ParseValue(
                value="KAPALI",
                raw_value="Bölümde TEFAS var fakat güncel listede kod yok",
                source_label="KAP_DETAIL_HTML + TEFAS:getFplFonList",
                evidence=truncate(section_text, 1600),
                confidence="YÜKSEK",
                matched_pattern=r"\btefas\b + TEFAS_LIST_NO_MATCH",
                matched_scope="ALIM_SATIM_BÖLÜMÜ",
                decision_reason="Bölümde TEFAS kelimesi bulundu; kod güncel işlem gören fon listesinde olmadığı için KAPALI.",
            )

        # Bölüm var ama alan/TEFAS beyanı yok: KAPALI.
        return ParseValue(
            value="KAPALI",
            raw_value="Alım Satım Yerleri ayrı çıkarılamadı; bölümde TEFAS kanıtı yok",
            source_label="KAP_DETAIL_HTML:Temel Alım Satım Bilgileri",
            evidence=truncate(section_text, 1600),
            confidence="YÜKSEK",
            matched_pattern="BOLUM_VAR_TEFAS_KANITI_YOK",
            matched_scope="ALIM_SATIM_BÖLÜMÜ",
            decision_reason="İlgili bölümde kesin AÇIK beyan bulunmadığı için KAPALI kabul edildi.",
        )

    # Sayfa yüklendi fakat ilgili alan/bölüm hiç parse edilemedi: teknik belirsizlik.
    return ParseValue(
        value="BİLİNMİYOR",
        raw_value="Alım Satım Yerleri alanı teknik olarak ayrıştırılamadı",
        source_label="KAP_DETAIL_HTML:Temel Alım Satım Bilgileri",
        evidence="",
        confidence="YOK",
        matched_pattern="ALAN_PARSE_EDILEMEDI",
        matched_scope="HTML",
        decision_reason="Alım Satım Yerleri alanı ve Temel Alım Satım Bilgileri bölümü DOM'da bulunamadı.",
    )


def run_internal_rule_self_test() -> tuple[int, int]:
    """Ana ağ isteklerinden önce karar kurallarının sentetik öz testini yapar."""
    samples = [
        (
            "AFO_TAM_PLATFORM",
            """<html><body><table>
            <tr><th>Alım Satım Saatleri</th><th>Alım Satım Yerleri</th><th>Nemalandırma Esasları</th></tr>
            <tr><td>09:00-17:00</td><td>Akbank Şubeleri, Türkiye Elektronik Fon Dağıtım Platformu</td><td>-</td></tr>
            </table></body></html>""",
            "AFO", {}, "AÇIK",
        ),
        (
            "ALE_BANKALAR",
            """<html><body><table>
            <tr><th>Alım Satım Saatleri</th><th>Alım Satım Yerleri</th><th>Nemalandırma Esasları</th></tr>
            <tr><td>09:00-17:00</td><td>Ak Portföy Yönetimi A.Ş., Akbank Şubeleri, Burgan Bank, Odeabank</td><td>-</td></tr>
            </table></body></html>""",
            "ALE", {}, "KAPALI",
        ),
        (
            "BOS_ALAN",
            """<html><body><table>
            <tr><th>Alım Satım Saatleri</th><th>Alım Satım Yerleri</th><th>Nemalandırma Esasları</th></tr>
            <tr><td>09:00-17:00</td><td></td><td>-</td></tr>
            </table></body></html>""",
            "ZZZ", {}, "KAPALI",
        ),
        (
            "SADECE_KURUCU",
            """<html><body><table>
            <tr><th>Alım Satım Saatleri</th><th>Alım Satım Yerleri</th><th>Nemalandırma Esasları</th></tr>
            <tr><td>09:00-17:00</td><td>Sadece Kurucu bünyesinde</td><td>-</td></tr>
            </table></body></html>""",
            "ZZZ", {}, "KAPALI",
        ),
        (
            "CIPLAK_TEFAS_LISTE_DOGRULAMA",
            """<html><body><table>
            <tr><th>Alım Satım Saatleri</th><th>Alım Satım Yerleri</th><th>Nemalandırma Esasları</th></tr>
            <tr><td>09:00-17:00</td><td>TEFAS, banka şubeleri</td><td>-</td></tr>
            </table></body></html>""",
            "JET", {"JET": {"durum": "AÇIK"}}, "AÇIK",
        ),
        (
            "CIPLAK_TEFAS_LISTEDE_YOK",
            """<html><body><table>
            <tr><th>Alım Satım Saatleri</th><th>Alım Satım Yerleri</th><th>Nemalandırma Esasları</th></tr>
            <tr><td>09:00-17:00</td><td>TEFAS, banka şubeleri</td><td>-</td></tr>
            </table></body></html>""",
            "ICF", {}, "KAPALI",
        ),
    ]

    passed = 0
    failures: list[str] = []
    for name, html, code, tefas_list, expected in samples:
        soup = clean_soup(html)
        result = extract_transaction(
            soup,
            visible_lines(soup),
            fund_code=code,
            tefas_traded_funds=tefas_list,
            tefas_list_error="",
        )
        if result.value == expected:
            passed += 1
        else:
            failures.append(f"{name}: beklenen={expected}, bulunan={result.value}, neden={result.decision_reason}")

    if failures:
        raise RuntimeError("Kural öz testi başarısız: " + " || ".join(failures))
    return passed, len(samples)


def verify_page_code(lines: Sequence[str], expected_code: str) -> str:
    expected = expected_code.upper()
    # İlk 250 görünür satır yeterlidir; sayfanın başında fon kodu yer alır.
    for line in lines[:250]:
        tokens = re.findall(r"\b[A-Z0-9]{2,8}\b", normalize_text(line).upper())
        if expected in tokens:
            return "EVET"
    return "HAYIR"


# -----------------------------------------------------------------------------
# KAP YATIRIMCI BİLGİ FORMU PDF YEDEĞİ
# -----------------------------------------------------------------------------

def find_investor_form_url(summary_html: str, base_url: str) -> str:
    soup = clean_soup(summary_html)
    for anchor in soup.find_all("a", href=True):
        text = normalize_text(anchor.get_text(" ", strip=True))
        href = normalize_text(anchor.get("href"))
        if href and contains_any(text, INVESTOR_FORM_LABELS):
            return urljoin(base_url, href)
    return ""


def pdf_text_from_bytes(content: bytes) -> str:
    payload = bytes(content or b"")
    if not payload.lstrip().startswith(b"%PDF"):
        prefix = payload[:12].hex(" ")
        raise ValueError(
            "KAP belge yanıtı gerçek PDF değil "
            f"(ilk baytlar: {prefix or 'boş yanıt'})."
        )

    reader = PdfReader(BytesIO(payload), strict=False)
    pages: list[str] = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n".join(pages)


def save_investor_form(fund: FundEntry, content: bytes, text: str) -> None:
    RAW_DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = RAW_DOCUMENT_DIR / f"{fund.fund_code}_YATIRIMCI_BILGI_FORMU.pdf"
    txt_path = RAW_DOCUMENT_DIR / f"{fund.fund_code}_YATIRIMCI_BILGI_FORMU.txt"
    pdf_path.write_bytes(content)
    txt_path.write_text(text, encoding="utf-8")


def extract_start_from_investor_form(text: str) -> ParseValue:
    normalized = normalize_text(text)
    date_expr = r"((?:0?[1-9]|[12]\d|3[01])\s*[./-]\s*(?:0?[1-9]|1[0-2])\s*[./-]\s*(?:19|20)\d{2})"
    patterns = (
        # Halka arz/satış/ihraç tarihleri kuruluş tarihinden önce denenir.
        ("Fonun halka arz tarihi", rf"Fon[’'`]?un\s+halka\s+arz\s+tarihi\s*:?\s*{date_expr}"),
        ("Halka arz tarihi", rf"Halka\s+arz\s+tarihi\s*:?\s*{date_expr}"),
        ("Fonun satış başlangıç tarihi", rf"Fon[’'`]?un\s+satış\s+başlangıç\s+tarihi\s*:?\s*{date_expr}"),
        ("Satış başlangıç tarihi", rf"Satış\s+başlangıç\s+tarihi\s*:?\s*{date_expr}"),
        (
            "Fon paylarının satış başlangıç tarihi",
            rf"Fon\s+paylarının\s+satışına\s+{date_expr}\s+tarihinde\s+başlanmıştır",
        ),
        ("İlk ihraç tarihi", rf"İlk\s+ihraç\s+tarihi\s*:?\s*{date_expr}"),
        ("İhraç tarihi", rf"İhraç\s*tarihi\s*:?\s*{date_expr}"),
        ("Fonun başlangıç tarihi", rf"Fon[’'`]?un\s+başlangıç\s+tarihi\s*:?\s*{date_expr}"),
        (
            "Fonun kuruluş tarihi",
            rf"Fon[’'`]?un\s+kuruluş\s+tarihi\s*:?\s*{date_expr}",
        ),
        ("Kuruluş tarihi", rf"Kuruluş\s+tarihi\s*:?\s*{date_expr}"),
    )

    for label, pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        dates = find_dates(match.group(0))
        if not dates:
            continue
        selected = dates[0]
        return ParseValue(
            value=str(selected.year),
            raw_value=format_date_tr(selected),
            source_label=f"KAP_YBF_PDF:{label}",
            evidence=truncate(match.group(0), 800),
            confidence="YÜKSEK",
        )

    return ParseValue(
        value="—",
        raw_value="",
        source_label="KAP_YBF_PDF:Başlangıç",
        evidence="Yatırımcı Bilgi Formu içinde ihraç/satış/kuruluş tarihi bulunamadı.",
        confidence="YOK",
    )


def extract_risk_from_investor_form(text: str) -> ParseValue:
    values, detail = _risk_values_from_text(text)
    if values:
        selected = max(values)
        value_detail = ", ".join(str(item) for item in values)
        return ParseValue(
            value=str(selected),
            raw_value=value_detail,
            source_label="KAP_YBF_PDF:Risk Değeri",
            evidence=truncate(detail or value_detail, 1000),
            confidence="ORTA" if len(values) == 1 else "YÜKSEK",
            decision_reason=(
                "PDF'de tek risk değeri bulundu."
                if len(values) == 1
                else f"PDF'de çoklu pay grubu riskleri bulundu ({value_detail}); ana değer olarak en yüksek risk {selected} seçildi."
            ),
        )
    return ParseValue(
        value="—",
        raw_value="",
        source_label="KAP_YBF_PDF:Risk Değeri",
        evidence="Yatırımcı Bilgi Formu içinde güvenilir risk değeri bulunamadı.",
        confidence="YOK",
    )


def extract_transaction_from_investor_form(text: str) -> ParseValue:
    """Yatırımcı Bilgi Formu PDF metninden TEFAS işlem durumunu çıkarır."""
    compact = compact_for_match(text)

    direct_open_patterns = (
        (
            r"katilma paylarinin alim ve satimi.{0,45}kurucunun yani sira.{0,120}"
            r"tefas(?: a)? uye olan fon dagitim kuruluslari araciligiyla(?: da)? "
            r"(?:yapilir|yapilmaktadir|gerceklestirilir|gerceklestirilmektedir)",
            "Katılma payları kurucunun yanı sıra TEFAS üyesi dağıtım kuruluşlarından alınır/satılır",
        ),
        (
            r"katilma paylarinin alim ve satimi.{0,45}kurucunun yani sira.{0,120}"
            r"turkiye elektronik fon (?:alim satim|dagitim) platformu(?:na| na)? "
            r"uye olan fon dagitim kuruluslari araciligiyla(?: da)? "
            r"(?:yapilir|yapilmaktadir|gerceklestirilir|gerceklestirilmektedir)",
            "Katılma payları kurucunun yanı sıra Türkiye Elektronik Fon Platformu üyelerinden alınır/satılır",
        ),
        (
            r"kurucunun yani sira.{0,160}(?:tefas(?: a)?|turkiye elektronik fon "
            r"(?:alim satim|dagitim) platformu(?:na| na)?) uye olan fon dagitim "
            r"kuruluslari araciligiyla(?: da)? "
            r"(?:yapilir|yapilmaktadir|gerceklestirilir|gerceklestirilmektedir)",
            "Kurucunun yanı sıra platform üyesi fon dağıtım kuruluşları aracılığıyla alım/satım",
        ),
    )

    for pattern, reason in direct_open_patterns:
        match = re.search(pattern, compact)
        if match:
            return ParseValue(
                value="AÇIK",
                raw_value=reason,
                source_label="KAP_YBF_PDF:Alım Satım ve Vergileme Esasları",
                evidence=truncate(match.group(0), 1200),
                confidence="ÇOK YÜKSEK",
                matched_pattern=pattern,
                matched_scope="PDF_DOĞRUDAN_ALIM_SATIM_CÜMLESİ",
                decision_reason="PDF doğrudan cümlesi kurucu yanında TEFAS/platform üyesi dağıtım kuruluşlarını açıkça belirtiyor.",
            )

    direct_closed_patterns = (
        r"katilma paylarinin alim ve satimi.{0,80}(?:yalnizca|sadece) kurucu(?:nun)? "
        r"(?:araciligiyla|nezdinde).{0,50}(?:yapilir|yapilmaktadir|gerceklestirilir)",
        r"fon paylarinin alim ve satimi.{0,80}(?:yalnizca|sadece) kurucu(?:nun)? "
        r"(?:araciligiyla|nezdinde).{0,50}(?:yapilir|yapilmaktadir|gerceklestirilir)",
    )
    for pattern in direct_closed_patterns:
        match = re.search(pattern, compact)
        if match:
            return ParseValue(
                value="KAPALI",
                raw_value="Katılma/fon paylarının alım satımı yalnızca kurucu üzerinden",
                source_label="KAP_YBF_PDF:Alım Satım ve Vergileme Esasları",
                evidence=truncate(match.group(0), 1200),
                confidence="ÇOK YÜKSEK",
                matched_pattern=pattern,
                matched_scope="PDF_DOĞRUDAN_ALIM_SATIM_CÜMLESİ",
                decision_reason="PDF doğrudan cümlesi işlemleri yalnızca kurucuyla sınırlandırıyor.",
            )

    negative_patterns = (
        r"turkiye elektronik fon (?:alim satim|dagitim) platformu.{0,120}islem gormemektedir",
        r"turkiye elektronik fon (?:alim satim|dagitim) platformu.{0,120}islem gormez",
        r"turkiye elektronik fon (?:alim satim|dagitim) platformu.{0,120}dahil degildir",
        r"elektronik fon (?:alim satim|dagitim) platformu.{0,120}islem gormemektedir",
        r"tefas.{0,100}islem gormemektedir",
        r"tefas.{0,100}islem gormez",
        r"tefas.{0,100}islem yapilmaz",
        r"tefas.{0,100}alim satima konu degildir",
        r"tefas.{0,100}acik degildir",
        r"tefas.{0,100}kapalidir",
        r"tefas.{0,100}dahil degildir",
        r"tefas disi",
        r"katilma paylarinin alim ve satimi.{0,120}(?:yalnizca|sadece) kurucu",
        r"fon paylarinin alim ve satimi.{0,120}(?:yalnizca|sadece) kurucu",
        r"(?:katilma|fon) paylari.{0,120}kurucu nezdinde.{0,120}(?:alinir|satilir|alim|satim)",
    )
    for pattern in negative_patterns:
        match = re.search(pattern, compact)
        if match:
            return ParseValue(
                value="KAPALI",
                raw_value="KAP Yatırımcı Bilgi Formunda açık olumsuz ifade",
                source_label="KAP_YBF_PDF:Alım Satım ve Vergileme Esasları",
                evidence=truncate(match.group(0), 1000),
                confidence="YÜKSEK",
                matched_pattern=pattern,
                matched_scope="PDF_GENEL_YEDEK",
                decision_reason="TEFAS/platform için açık ve işlem bağlamlı olumsuz ifade bulundu.",
            )

    positive_patterns = (
        r"turkiye elektronik fon (?:alim satim|dagitim) platformu.{0,120}islem gormektedir",
        r"turkiye elektronik fon (?:alim satim|dagitim) platformu.{0,120}islem gorur",
        r"elektronik fon (?:alim satim|dagitim) platformu.{0,120}islem gormektedir",
        r"tefas.{0,100}islem gormektedir",
        r"tefas.{0,100}islem gorur",
        r"tefas.{0,100}alim satima aciktir",
        r"tefas.{0,100}alim satima konu",
        r"tefas(?: a)? uye olan fon dagitim kuruluslari",
        r"tefas uye fon dagitim kuruluslari",
        r"tefas uzerinden",
        r"tefas araciligiyla",
        r"tefas ta gerceklesen islemler",
        r"platform uzerinden.{0,100}(?:alim|satim|islem)",
        r"turkiye elektronik fon (?:alim satim|dagitim) platformu(?:na| na)? uye olan fon dagitim kuruluslari",
    )
    for pattern in positive_patterns:
        match = re.search(pattern, compact)
        if match:
            return ParseValue(
                value="AÇIK",
                raw_value="KAP Yatırımcı Bilgi Formunda açık olumlu ifade",
                source_label="KAP_YBF_PDF:Alım Satım ve Vergileme Esasları",
                evidence=truncate(match.group(0), 1000),
                confidence="YÜKSEK",
                matched_pattern=pattern,
                matched_scope="PDF_GENEL_YEDEK",
                decision_reason="TEFAS veya platform erişimini destekleyen olumlu ifade bulundu.",
            )

    return ParseValue(
        value="BİLİNMİYOR",
        raw_value="PDF içinde kesin TEFAS kanıtı bulunamadı",
        source_label="KAP_YBF_PDF:Alım Satım ve Vergileme Esasları",
        evidence="",
        confidence="DÜŞÜK",
        matched_scope="PDF_GENEL_YEDEK",
        decision_reason="AÇIK veya KAPALI için yeterince kesin cümle bulunamadı.",
    )

# -----------------------------------------------------------------------------
# VERİTABANI KARŞILAŞTIRMASI
# -----------------------------------------------------------------------------

def discover_db() -> Path | None:
    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.extend([
            Path(local_app_data) / "FonPulse" / "Data" / "fonpulse.db",
            Path(local_app_data) / "PiyasaNabziTurkiye" / "Data" / "fonpulse.db",
        ])
    candidates.extend([
        BASE_DIR / "fonpulse.db",
        BASE_DIR.parent / "fonpulse.db",
        Path.home() / "Desktop" / "fonpulse.db",
        Path.home() / "Downloads" / "fonpulse.db",
    ])
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def load_db_values(db_path: Path) -> dict[str, dict[str, str]]:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "fund_catalog" not in tables:
            raise RuntimeError("fund_catalog tablosu bulunamadı.")

        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(fund_catalog)").fetchall()
        }
        required = {"fund_code", "start_year", "risk_level", "transaction_status"}
        missing = required - columns
        if missing:
            raise RuntimeError(f"fund_catalog eksik sütunlar: {sorted(missing)}")

        select_columns = ["fund_code", "start_year", "risk_level", "transaction_status"]
        if "fund_name" in columns:
            select_columns.append("fund_name")
        if "updated_at" in columns:
            select_columns.append("updated_at")

        query = "SELECT " + ", ".join(select_columns) + " FROM fund_catalog"
        params: list[Any] = []
        if "tefas_kind" in columns:
            query += " WHERE UPPER(COALESCE(tefas_kind,'')) = ?"
            params.append("YAT")

        rows = connection.execute(query, params).fetchall()
    finally:
        connection.close()

    result: dict[str, dict[str, str]] = {}
    for row in rows:
        code = normalize_text(row["fund_code"]).upper()
        if not code:
            continue
        keys = set(row.keys())
        result[code] = {
            "start_year": normalize_text(row["start_year"]),
            "risk_level": normalize_text(row["risk_level"]),
            "transaction_status": normalize_text(row["transaction_status"]),
            "fund_name": normalize_text(row["fund_name"]) if "fund_name" in keys else "",
            "updated_at": normalize_text(row["updated_at"]) if "updated_at" in keys else "",
        }
    return result


# -----------------------------------------------------------------------------
# TEK FON TESTİ
# -----------------------------------------------------------------------------

def save_html(fund: FundEntry, html: str) -> None:
    RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_HTML_DIR / f"{fund.fund_code}_{fund.fund_permalink or 'detay'}.html"
    path.write_text(html, encoding="utf-8")


def test_one_fund(
    fund: FundEntry,
    db_values: dict[str, dict[str, str]],
    tefas_traded_funds: dict[str, dict[str, str]],
    tefas_list_error: str,
    rate_limiter: GlobalRateLimiter,
    save_raw_html: bool,
) -> FundResult:
    now = datetime.now().isoformat(timespec="seconds")
    db = db_values.get(fund.fund_code, {})

    investor_form_url = ""
    investor_form_http_status: int | None = None
    fallback_used: list[str] = []
    fallback_error = ""
    fallback_attempted = "HAYIR"

    try:
        session = thread_session()
        response, elapsed_ms = request_with_retry(
            session,
            fund.detail_url,
            rate_limiter=rate_limiter,
        )
        response.encoding = response.apparent_encoding or response.encoding or "utf-8"
        html = response.text

        soup = clean_soup(html)
        lines = visible_lines(soup)

        start = extract_start(soup, lines)
        risk = extract_risk(soup, lines)
        transaction = extract_transaction(
            soup,
            lines,
            fund_code=fund.fund_code,
            tefas_traded_funds=tefas_traded_funds,
            tefas_list_error=tefas_list_error,
        )
        code_verified = verify_page_code(lines, fund.fund_code)

        # Genel Bilgiler sayfası eksik kalırsa aynı fonun KAP Özet sayfasındaki
        # Yatırımcı Bilgi Formu PDF'i yalnızca eksik alanlar için yedek kaynaktır.
        need_pdf_fallback = (
            start.value == "—"
            or risk.value == "—"
            or transaction.value == "BİLİNMİYOR"
        )
        if need_pdf_fallback:
            fallback_attempted = "EVET"
            try:
                summary_response, _ = request_with_retry(
                    session,
                    fund.summary_url,
                    rate_limiter=rate_limiter,
                )
                summary_response.encoding = (
                    summary_response.apparent_encoding
                    or summary_response.encoding
                    or "utf-8"
                )
                investor_form_url = find_investor_form_url(
                    summary_response.text,
                    summary_response.url,
                )

                if investor_form_url:
                    pdf_response, _ = request_with_retry(
                        session,
                        investor_form_url,
                        rate_limiter=rate_limiter,
                        timeout=70,
                    )
                    investor_form_http_status = pdf_response.status_code
                    pdf_text = pdf_text_from_bytes(pdf_response.content)

                    # Public GitHub çalışmasında ham belge depolanmaz.
                    if SAVE_DIAGNOSTIC_FILES:
                        save_investor_form(fund, pdf_response.content, pdf_text)

                    if start.value == "—":
                        pdf_start = extract_start_from_investor_form(pdf_text)
                        if pdf_start.value != "—":
                            start = pdf_start
                            fallback_used.append("BAŞLANGIÇ")

                    if risk.value == "—":
                        pdf_risk = extract_risk_from_investor_form(pdf_text)
                        if pdf_risk.value != "—":
                            risk = pdf_risk
                            fallback_used.append("RİSK")

                    if transaction.value == "BİLİNMİYOR":
                        pdf_transaction = extract_transaction_from_investor_form(pdf_text)
                        if pdf_transaction.value != "BİLİNMİYOR":
                            transaction = pdf_transaction
                            fallback_used.append("İŞLEM")
                else:
                    fallback_error = (
                        "KAP Özet sayfasında Yatırımcı Bilgi Formu bağlantısı bulunamadı."
                    )
            except Exception as pdf_exc:
                fallback_error = f"{type(pdf_exc).__name__}: {pdf_exc}"

        should_save = (
            save_raw_html
            or code_verified == "HAYIR"
            or start.value == "—"
            or risk.value == "—"
            or transaction.value == "BİLİNMİYOR"
            or transaction.conflict_flag == "EVET"
        )
        if should_save and SAVE_DIAGNOSTIC_FILES:
            save_html(fund, html)

        db_start = normalize_text(db.get("start_year"))
        db_risk = normalize_text(db.get("risk_level"))
        db_trade = normalize_text(db.get("transaction_status"))
        tefas_row = tefas_traded_funds.get(fund.fund_code, {})

        parse_method = "KAP_LIST_JSON + KAP_DETAIL_VISIBLE_HTML + TEFAS_TRADED_LIST_JSON"
        if investor_form_url:
            parse_method += " + KAP_SUMMARY_HTML + KAP_YBF_PDF"

        return FundResult(
            test_time=now,
            fund_code=fund.fund_code,
            fund_name=fund.fund_name,
            fund_oid=fund.fund_oid,
            fund_permalink=fund.fund_permalink,
            detail_url=fund.detail_url,
            final_url=response.url,
            summary_url=fund.summary_url,
            investor_form_url=investor_form_url,
            investor_form_http_status=investor_form_http_status,
            fallback_used=", ".join(fallback_used) or "—",
            fallback_error=fallback_error,
            http_status=response.status_code,
            response_ms=elapsed_ms,
            page_code_verified=code_verified,

            start_date=start.raw_value or "—",
            start_year=start.value,
            start_source=start.source_label,
            start_evidence=start.evidence,
            start_confidence=start.confidence,

            risk_level=risk.value,
            risk_detail=risk.raw_value or risk.value,
            risk_multi_value="EVET" if "," in (risk.raw_value or "") else "HAYIR",
            risk_source=risk.source_label,
            risk_evidence=risk.evidence,
            risk_confidence=risk.confidence,

            transaction_status=transaction.value,
            transaction_source=transaction.source_label,
            transaction_evidence=transaction.evidence,
            transaction_confidence=transaction.confidence,
            transaction_matched_pattern=transaction.matched_pattern or "—",
            transaction_matched_scope=transaction.matched_scope or "—",
            transaction_decision_reason=transaction.decision_reason or "—",
            transaction_conflict_flag=transaction.conflict_flag or "HAYIR",
            tefas_traded_list_match=("EVET" if fund.fund_code in tefas_traded_funds else "HAYIR") if not tefas_list_error else "HATA",
            tefas_traded_list_status=normalize_text(tefas_row.get("durum")) or ("—" if not tefas_list_error else "HATA"),
            tefas_traded_list_title=normalize_text(tefas_row.get("unvan")) or "—",
            tefas_traded_list_date=normalize_text(tefas_row.get("tarih")) or "—",
            fallback_attempted=fallback_attempted,
            fallback_winner=", ".join(fallback_used) or "—",

            db_start_year=db_start or "—",
            db_risk_level=db_risk or "—",
            db_transaction_status=db_trade or "—",
            compare_start=compare_values(start.value, db_start),
            compare_risk=compare_values(risk.value, db_risk),
            compare_transaction=compare_values(
                transaction.value,
                db_trade,
                normalizer=normalize_status,
            ),

            parse_method=parse_method,
            error="",
        )
    except Exception as exc:
        db_start = normalize_text(db.get("start_year"))
        db_risk = normalize_text(db.get("risk_level"))
        db_trade = normalize_text(db.get("transaction_status"))
        tefas_row = tefas_traded_funds.get(fund.fund_code, {})
        return FundResult(
            test_time=now,
            fund_code=fund.fund_code,
            fund_name=fund.fund_name,
            fund_oid=fund.fund_oid,
            fund_permalink=fund.fund_permalink,
            detail_url=fund.detail_url,
            final_url="",
            summary_url=fund.summary_url,
            investor_form_url=investor_form_url,
            investor_form_http_status=investor_form_http_status,
            fallback_used=", ".join(fallback_used) or "—",
            fallback_error=fallback_error,
            http_status=None,
            response_ms=None,
            page_code_verified="HAYIR",

            start_date="—",
            start_year="—",
            start_source="KAP_DETAIL_HTML:Fonun Halka Arz Tarihi",
            start_evidence="",
            start_confidence="YOK",

            risk_level="—",
            risk_detail="—",
            risk_multi_value="HAYIR",
            risk_source="KAP_DETAIL_HTML:Risk Değeri",
            risk_evidence="",
            risk_confidence="YOK",

            transaction_status="BİLİNMİYOR",
            transaction_source="KAP_DETAIL_HTML:Temel Alım Satım Bilgileri",
            transaction_evidence="",
            transaction_confidence="YOK",
            transaction_matched_pattern="—",
            transaction_matched_scope="—",
            transaction_decision_reason="İstek veya ayrıştırma hatası nedeniyle karar üretilemedi.",
            transaction_conflict_flag="HAYIR",
            tefas_traded_list_match=("EVET" if fund.fund_code in tefas_traded_funds else "HAYIR") if not tefas_list_error else "HATA",
            tefas_traded_list_status=normalize_text(tefas_row.get("durum")) or ("—" if not tefas_list_error else "HATA"),
            tefas_traded_list_title=normalize_text(tefas_row.get("unvan")) or "—",
            tefas_traded_list_date=normalize_text(tefas_row.get("tarih")) or "—",
            fallback_attempted=fallback_attempted,
            fallback_winner=", ".join(fallback_used) or "—",

            db_start_year=db_start or "—",
            db_risk_level=db_risk or "—",
            db_transaction_status=db_trade or "—",
            compare_start=compare_values("", db_start),
            compare_risk=compare_values("", db_risk),
            compare_transaction=compare_values(
                "",
                db_trade,
                normalizer=normalize_status,
            ),

            parse_method=(
                "KAP_LIST_JSON + KAP_DETAIL_VISIBLE_HTML + TEFAS_TRADED_LIST_JSON + KAP_YBF_PDF_FALLBACK"
            ),
            error=f"{type(exc).__name__}: {exc}",
        )

# -----------------------------------------------------------------------------
# ÇIKTI / ÖZET
# -----------------------------------------------------------------------------

def write_csv(path: Path, results: Sequence[FundResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(asdict(results[0]).keys()) if results else list(FundResult.__annotations__.keys())
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            writer.writerow(asdict(result))


def write_json(path: Path, results: Sequence[FundResult]) -> None:
    path.write_text(
        json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_summary(
    *,
    total_kap_funds: int,
    selected_count: int,
    db_path: Path | None,
    results: Sequence[FundResult],
    elapsed_seconds: float,
    tefas_traded_count: int,
    tefas_list_error: str,
) -> str:
    ok_http = sum(1 for item in results if item.http_status == 200 and not item.error)
    errors = sum(1 for item in results if item.error)
    code_verified = sum(1 for item in results if item.page_code_verified == "EVET")

    start_found = sum(1 for item in results if item.start_year not in {"", "—"})
    risk_found = sum(1 for item in results if item.risk_level not in {"", "—"})
    open_found = sum(1 for item in results if item.transaction_status == "AÇIK")
    closed_found = sum(1 for item in results if item.transaction_status == "KAPALI")
    unknown_found = sum(1 for item in results if item.transaction_status == "BİLİNMİYOR")

    start_matches = sum(1 for item in results if item.compare_start == "EŞLEŞTİ")
    risk_matches = sum(1 for item in results if item.compare_risk == "EŞLEŞTİ")
    trade_matches = sum(1 for item in results if item.compare_transaction == "EŞLEŞTİ")

    missing_start_codes = [item.fund_code for item in results if item.start_year in {"", "—"}]
    missing_risk_codes = [item.fund_code for item in results if item.risk_level in {"", "—"}]
    unknown_trade_codes = [item.fund_code for item in results if item.transaction_status == "BİLİNMİYOR"]
    conflict_trade_codes = [item.fund_code for item in results if item.transaction_conflict_flag == "EVET"]
    trade_mismatch_codes = [
        item.fund_code for item in results
        if item.compare_transaction == "FARKLI"
    ]
    error_codes = [item.fund_code for item in results if item.error]

    def code_list(values: Sequence[str]) -> str:
        return ", ".join(values) if values else "YOK"

    lines = [
        "KAP YAT DETAY + TEFAS İŞLEM LİSTESİ DOĞRULAMA TESTİ v8",
        "=" * 70,
        f"Test zamanı                 : {datetime.now().isoformat(timespec='seconds')}",
        f"Script sürümü               : {SCRIPT_VERSION}",
        f"Çalışan dosya               : {Path(__file__).resolve()}",
        f"KAP YF aktif ana liste      : {total_kap_funds}",
        f"TEFAS işlem gören liste     : {tefas_traded_count if not tefas_list_error else 'ERİŞİM HATASI'}",
        f"Seçilen fon                 : {selected_count}",
        f"HTTP 200 başarılı           : {ok_http}",
        f"Sayfa kodu doğrulanan       : {code_verified}",
        f"Hata                        : {errors}",
        f"Toplam süre                 : {elapsed_seconds:.1f} saniye",
        f"Yerel DB                    : {db_path if db_path else 'BULUNAMADI'}",
        "",
        "KAP DETAY SAYFASINDA BULUNAN",
        "-" * 35,
        f"Başlangıç yılı              : {start_found}",
        f"Risk seviyesi               : {risk_found}",
        f"İşlem AÇIK                  : {open_found}",
        f"İşlem KAPALI                : {closed_found}",
        f"İşlem BİLİNMİYOR            : {unknown_found}",
        "",
        "YEREL DB İLE EŞLEŞEN",
        "-" * 35,
        f"Başlangıç                   : {start_matches}",
        f"Risk                        : {risk_matches}",
        f"İşlem                       : {trade_matches}",
        "",
        "EKSİK / BELİRSİZ / UYUŞMAZ KAYITLAR",
        "-" * 35,
        f"Başlangıç bulunamayan       : {code_list(missing_start_codes)}",
        f"Risk bulunamayan            : {code_list(missing_risk_codes)}",
        f"İşlem BİLİNMİYOR            : {code_list(unknown_trade_codes)}",
        f"İşlem çakışması              : {code_list(conflict_trade_codes)}",
        f"Yerel DB işlem farkı         : {code_list(trade_mismatch_codes)}",
        f"Hata veren fon              : {code_list(error_codes)}",
        "",
        "KAYNAK MANTIĞI",
        "-" * 35,
        "Başlangıç : KAP Genel Bilgiler > Fonun Halka Arz Tarihi; eksikse Yatırımcı Bilgi Formu PDF",
        "Risk      : KAP Genel Bilgiler > Risk Değeri; eksikse Yatırımcı Bilgi Formu PDF",
        "İşlem     : KAP Genel Bilgiler > Alım Satım Yerleri; çıplak TEFAS varsa TEFAS işlem gören fon listesi",
        "",
        "NOT",
        "-" * 35,
        "Script/React veri blokları ayrıştırma öncesinde kaldırılmıştır.",
        "Alım Satım Yerleri boş/Bilgi Mevcut Değil ise KAPALI kabul edilir.",
        "Alan yalnızca kurucu, portföy yönetim şirketi veya banka kanalları içeriyorsa KAPALI kabul edilir.",
        "'Sadece Kurucu bünyesinde' ve açık 'TEFAS'ta işlem görmemektedir' beyanları KAPALI kabul edilir.",
        "Kurucu yanında TEFAS/TEFDP üyesi fon dağıtım kuruluşları açıkça yazıyorsa AÇIK kabul edilir.",
        "Yalnız çıplak TEFAS kelimesi varsa güncel TEFAS İşlem Gören Yatırım Fonları listesiyle doğrulanır.",
        "Başlangıç tarihi etiket önceliğiyle seçilir; bölümdeki ilgisiz en eski tarih alınmaz.",
        "Çoklu pay grubu risklerinde tüm değerler korunur ve ana risk olarak en yüksek değer seçilir.",
        "BİLİNMİYOR yalnızca teknik erişim veya DOM ayrıştırma hatasında bırakılır.",
        "Tüm eski alternatif kelimeler ve PDF fallback metinleri korunmuştur.",
    ]
    return "\n".join(lines)


def print_result_line(index: int, total: int, result: FundResult) -> None:
    status = "OK" if not result.error else "HATA"
    print(
        f"[{index:>4}/{total}] {result.fund_code:<6} {status:<4} | "
        f"Başlangıç {result.start_year:<4} | Risk {result.risk_level:<2} | "
        f"İşlem {result.transaction_status:<10} | HTTP {result.http_status or '—'}"
    )
    if result.error:
        print(f"       {result.error}")


# -----------------------------------------------------------------------------
# KOMUT SATIRI / ANA AKIŞ
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KAP YAT fon detaylarından başlangıç, risk ve TEFAS işlem durumu testi."
    )
    parser.add_argument(
        "--codes",
        default="",
        help="Virgülle ayrılmış fon kodları. Örnek: PHE,TLY,AFO",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="KAP YF aktif listedeki tüm fonları test eder.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Seçilen fon sayısını sınırlar. 0 = sınır yok.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Eşzamanlı işçi sayısı. Varsayılan: {DEFAULT_WORKERS}",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"KAP istekleri arası ortak minimum saniye. Varsayılan: {DEFAULT_DELAY_SECONDS}",
    )
    parser.add_argument(
        "--save-html",
        action="store_true",
        help="Başarılı sayfalar dahil tüm ham HTML dosyalarını kaydeder.",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Yerel fonpulse.db karşılaştırmasını kapatır.",
    )
    return parser.parse_args()


def interactive_selection() -> tuple[bool, list[str]]:
    print("\nÇALIŞMA MODU")
    print(f"1 - Hızlı doğrulama testi ({len(DEFAULT_CODES)} fon: {', '.join(DEFAULT_CODES)})")
    print("2 - Belirli fon kodları")
    print("3 - Tüm KAP YAT fonları")
    choice = input("Seçim [1]: ").strip() or "1"

    if choice == "3":
        confirmation = input(
            "Tüm fonların detay sayfası tek tek taranacak. Devam için ALL yazın: "
        ).strip().upper()
        if confirmation != "ALL":
            print("Tüm tarama iptal edildi; hızlı örnek test kullanılacak.")
            return False, DEFAULT_CODES.copy()
        return True, []

    if choice == "2":
        entered = input("Fon kodlarını virgülle girin: ").strip()
        codes = [normalize_text(item).upper() for item in entered.split(",") if normalize_text(item)]
        return False, list(dict.fromkeys(codes)) or DEFAULT_CODES.copy()

    return False, DEFAULT_CODES.copy()


def select_funds(
    all_funds: Sequence[FundEntry],
    *,
    scan_all: bool,
    codes: Sequence[str],
    limit: int,
) -> tuple[list[FundEntry], list[str]]:
    by_code = {item.fund_code: item for item in all_funds}
    missing: list[str] = []

    if scan_all:
        selected = list(all_funds)
    else:
        selected = []
        for raw_code in codes:
            code = normalize_text(raw_code).upper()
            if not code:
                continue
            fund = by_code.get(code)
            if fund is None:
                missing.append(code)
            else:
                selected.append(fund)

    # Tekilleştir.
    unique: dict[str, FundEntry] = {}
    for fund in selected:
        unique.setdefault(fund.fund_code, fund)
    selected = list(unique.values())

    if limit and limit > 0:
        selected = selected[:limit]
    return selected, missing


def main() -> int:
    args = parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 90)
    print(f"KAP YAT HAM + DETAY + TEFAS İŞLEM LİSTESİ DOĞRULAMA TESTİ {SCRIPT_VERSION}")
    print("=" * 90)
    print(f"Script sürümü     : {SCRIPT_VERSION}")
    print(f"Çalışan dosya     : {Path(__file__).resolve()}")
    print(f"Hızlı test listesi: {len(DEFAULT_CODES)} fon")
    print("Kural öz testi    : çalıştırılıyor...")
    passed, total = run_internal_rule_self_test()
    print(f"Kural öz testi    : {passed}/{total} BAŞARILI")
    print("KAP YF aktif ana liste indiriliyor...")

    funds, raw_rows = fetch_kap_fund_list()
    print(f"KAP YF aktif fon sayısı: {len(funds)}")

    raw_json_path = OUTPUT_DIR / "KAP_YF_Y_HAM_CEVAP.json"
    raw_json_path.write_text(
        json.dumps(raw_rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("TEFAS İşlem Gören Yatırım Fonları listesi indiriliyor...")
    tefas_traded_funds: dict[str, dict[str, str]] = {}
    tefas_raw_payload: dict[str, Any] = {}
    tefas_list_error = ""
    try:
        tefas_traded_funds, tefas_raw_payload = fetch_tefas_traded_funds()
        print(f"TEFAS işlem gören fon sayısı: {len(tefas_traded_funds)}")
    except Exception as exc:
        tefas_list_error = f"{type(exc).__name__}: {exc}"
        print(f"TEFAS işlem listesi alınamadı: {tefas_list_error}")

    tefas_raw_path = OUTPUT_DIR / "TEFAS_ISLEM_GOREN_YATIRIM_FONLARI.json"
    tefas_raw_path.write_text(
        json.dumps(tefas_raw_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    scan_all = bool(args.all)
    codes = [
        normalize_text(item).upper()
        for item in args.codes.split(",")
        if normalize_text(item)
    ]

    if not scan_all and not codes:
        if sys.stdin.isatty():
            scan_all, codes = interactive_selection()
        else:
            codes = DEFAULT_CODES.copy()

    selected, missing_codes = select_funds(
        funds,
        scan_all=scan_all,
        codes=codes,
        limit=max(0, args.limit),
    )

    if missing_codes:
        print("KAP aktif YF listesinde bulunamayan kodlar: " + ", ".join(missing_codes))
    if not selected:
        raise RuntimeError("Test edilecek geçerli fon bulunamadı.")

    print(f"Test edilecek fon: {len(selected)}")
    print(f"İşçi sayısı       : {max(1, min(args.workers, 4))}")
    print(f"İstek aralığı     : {max(0.0, args.delay):.2f} saniye")

    db_path: Path | None = None
    db_values: dict[str, dict[str, str]] = {}
    if not args.no_db:
        db_path = discover_db()
        if db_path:
            try:
                db_values = load_db_values(db_path)
                print(f"Yerel DB          : {db_path}")
                print(f"Yerel YAT kayıt   : {len(db_values)}")
            except Exception as exc:
                print(f"Yerel DB okunamadı: {type(exc).__name__}: {exc}")
                db_path = None
        else:
            print("Yerel fonpulse.db bulunamadı; yalnızca KAP testi yapılacak.")

    rate_limiter = GlobalRateLimiter(max(0.0, args.delay))
    workers = max(1, min(int(args.workers), 4))
    # Küçük testlerde ham HTML otomatik saklanır; tüm taramada yalnızca --save-html.
    save_raw_html = bool(args.save_html or len(selected) <= 20)

    started = time.perf_counter()
    results: list[FundResult] = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                test_one_fund,
                fund,
                db_values,
                tefas_traded_funds,
                tefas_list_error,
                rate_limiter,
                save_raw_html,
            ): fund
            for fund in selected
        }

        completed = 0
        for future in as_completed(future_map):
            completed += 1
            result = future.result()
            results.append(result)
            print_result_line(completed, len(selected), result)

    results.sort(key=lambda item: item.fund_code)
    elapsed_seconds = time.perf_counter() - started

    csv_path = OUTPUT_DIR / "KAP_YAT_DETAY_ALAN_TESTI.csv"
    json_path = OUTPUT_DIR / "KAP_YAT_DETAY_ALAN_TESTI.json"
    summary_path = OUTPUT_DIR / "KAP_YAT_DETAY_TEST_OZET.txt"

    write_csv(csv_path, results)
    write_json(json_path, results)
    summary = build_summary(
        total_kap_funds=len(funds),
        selected_count=len(selected),
        db_path=db_path,
        results=results,
        elapsed_seconds=elapsed_seconds,
        tefas_traded_count=len(tefas_traded_funds),
        tefas_list_error=tefas_list_error,
    )
    summary_path.write_text(summary, encoding="utf-8")

    print("\n" + summary)
    print("\nÇIKTILAR")
    print(f"CSV          : {csv_path}")
    print(f"JSON         : {json_path}")
    print(f"ÖZET         : {summary_path}")
    print(f"KAP HAM LİSTE: {raw_json_path}")
    print(f"TEFAS İŞLEM  : {tefas_raw_path}")
    if save_raw_html:
        print(f"HAM HTML     : {RAW_HTML_DIR}")
        print(f"HAM BELGELER : {RAW_DOCUMENT_DIR}")

    if sys.stdin.isatty():
        input("\nKapatmak için Enter tuşuna basın...")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nİşlem kullanıcı tarafından durduruldu.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nHATA: {type(exc).__name__}: {exc}")
        if sys.stdin.isatty():
            input("Kapatmak için Enter tuşuna basın...")
        raise SystemExit(1)
