#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
KAP YAT HAM + DETAY + TEFAS İŞLEM LİSTESİ DOĞRULAMA TESTİ v9.3 DİKEY KOLON RİSK
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
    python kap_yat_ham_kaynak_testi.py --all --limit 300 --resume
    python kap_yat_ham_kaynak_testi.py --all --resume
    python kap_yat_ham_kaynak_testi.py --all --retry-only --resume
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

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / ".run_output" / "KAP_YAT_SOURCE"
RAW_HTML_DIR = OUTPUT_DIR / "HAM_SAYFALAR"
RAW_DOCUMENT_DIR = OUTPUT_DIR / "HAM_BELGELER"
STAGING_DIR = OUTPUT_DIR / "STAGING"
DIAGNOSTICS_DIR = OUTPUT_DIR / "DIAGNOSTICS"
PROGRESS_PATH = STAGING_DIR / "yat_kap_progress.json"
FAILED_CODES_PATH = STAGING_DIR / "failed_codes.json"
REQUEST_FAILURES_PATH = DIAGNOSTICS_DIR / "request_failures.json"
RUN_STATE_PATH = DIAGNOSTICS_DIR / "run_state.json"
ATTEMPT_EVENTS_PATH = DIAGNOSTICS_DIR / "attempt_events.jsonl"

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
DEFAULT_WORKERS = 1
DEFAULT_DELAY_SECONDS = 1.35
DEFAULT_DIAGNOSTIC_LIMIT = 300
SLOW_RETRY_DELAY_SECONDS = 2.75
ROUTINE_REQUEST_LIMIT = 65
ROUTINE_COOLDOWN_SECONDS = 180
RATE_LIMIT_COOLDOWNS = (180, 600, 1200)
BLOCK_COOLDOWN_SECONDS = 600
REQUEST_TIMEOUT_SECONDS = 50
MAX_RETRIES = 4
DEFAULT_RETRY_ROUNDS = 3
SCRIPT_VERSION = "v9.4-multi-source-risk-start-1"

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
    """KAP istek hızını, periyodik molaları ve 429 soğumasını tek merkezden yönetir."""

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        routine_request_limit: int = ROUTINE_REQUEST_LIMIT,
        routine_cooldown_seconds: int = ROUTINE_COOLDOWN_SECONDS,
    ) -> None:
        self.min_interval = max(0.0, float(min_interval_seconds))
        self.routine_request_limit = max(0, int(routine_request_limit))
        self.routine_cooldown_seconds = max(0, int(routine_cooldown_seconds))
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._requests_since_pause = 0
        self._consecutive_429 = 0
        self._pause_until = 0.0
        self._pause_reason = ""

    def _set_pause_locked(self, seconds: float, reason: str) -> None:
        seconds = max(0.0, float(seconds))
        candidate = time.monotonic() + seconds
        if candidate > self._pause_until:
            self._pause_until = candidate
            self._pause_reason = reason
        self._requests_since_pause = 0

    def _wait_for_pause_locked(self) -> None:
        announced = False
        while True:
            remaining = self._pause_until - time.monotonic()
            if remaining <= 0:
                if announced:
                    print("Bekleme tamamlandı; tarama otomatik devam ediyor.", flush=True)
                self._pause_until = 0.0
                self._pause_reason = ""
                return
            if not announced:
                print(
                    f"\nKORUMA BEKLEMESİ: {self._pause_reason} | "
                    f"yaklaşık {int(remaining) + 1} saniye.",
                    flush=True,
                )
                announced = True
            sleep_for = min(30.0, remaining)
            time.sleep(sleep_for)
            remaining_after = self._pause_until - time.monotonic()
            if remaining_after > 0:
                print(f"  Kalan bekleme: yaklaşık {int(remaining_after) + 1} saniye", flush=True)

    def wait(self) -> None:
        with self._lock:
            if (
                self.routine_request_limit > 0
                and self._requests_since_pause >= self.routine_request_limit
            ):
                self._set_pause_locked(
                    self.routine_cooldown_seconds,
                    f"{self.routine_request_limit} KAP isteği tamamlandı; düzenli soğuma molası",
                )
            self._wait_for_pause_locked()
            now = time.monotonic()
            wait_seconds = self.min_interval - (now - self._last_request)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            self._last_request = time.monotonic()
            self._requests_since_pause += 1

    def record_response(self, status_code: int, retry_after: str = "") -> None:
        with self._lock:
            if status_code == 429:
                self._consecutive_429 += 1
                parsed_retry = float(retry_after) if normalize_text(retry_after).isdigit() else 0.0
                index = min(self._consecutive_429 - 1, len(RATE_LIMIT_COOLDOWNS) - 1)
                cooldown = max(parsed_retry, float(RATE_LIMIT_COOLDOWNS[index]))
                self._set_pause_locked(
                    cooldown,
                    f"KAP HTTP 429 hız sınırı ({self._consecutive_429}. ardışık olay)",
                )
            elif status_code == 403:
                self._set_pause_locked(
                    BLOCK_COOLDOWN_SECONDS,
                    "KAP HTTP 403 erişim engeli; güvenli soğuma",
                )
            elif 200 <= status_code < 400:
                self._consecutive_429 = 0

    def record_network_error(self) -> None:
        with self._lock:
            self._set_pause_locked(45, "Geçici ağ hatası; kısa bağlantı molası")


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
    last_status: int | None = None

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
            last_status = response.status_code

            retry_after = normalize_text(response.headers.get("Retry-After"))
            if rate_limiter is not None:
                rate_limiter.record_response(response.status_code, retry_after)

            if response.status_code in {429, 403}:
                if rate_limiter is None:
                    fallback_wait = float(retry_after) if retry_after.isdigit() else (180.0 if response.status_code == 429 else 600.0)
                    time.sleep(fallback_wait)
                continue

            if response.status_code >= 500 and attempt < MAX_RETRIES:
                time.sleep(attempt * 5.0)
                continue

            response.raise_for_status()
            return response, elapsed_ms
        except Exception as exc:
            last_error = exc
            if rate_limiter is not None and last_status is None:
                rate_limiter.record_network_error()
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 3.0)

    category = f"HTTP_{last_status}" if last_status is not None else type(last_error).__name__
    raise RuntimeError(
        f"{category}: İstek başarısız: {url} | "
        f"{type(last_error).__name__ if last_error else 'HTTPError'}: {last_error or 'yanıt başarısız'}"
    )



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
    """Açık risk etiketi veya tek başına risk rakamını çıkarır.

    TL/USD/EUR ve A/B/C/D/pay grubu yakınlığındaki rakamlar bilinçli olarak
    risk adayı yapılmaz. Bu bağlamlar strateji metnindeki sıradan sayıları yanlış
    pozitif üretebildiği için v9.4'te tamamen kaldırılmıştır.
    """
    compact = compact_for_match(value)
    values: list[int] = []
    details: list[str] = []

    contextual_patterns = (
        r"(?:risk degeri|risk seviyesi|risk gostergesi|risk sinifi|risk grubu)\s*[:：=\-]?\s*([1-7])(?:\s*/\s*7)?(?:\b|$)",
        r"\b([1-7])\s*/\s*7\b",
    )
    for pattern in contextual_patterns:
        for match in re.finditer(pattern, compact):
            number = int(match.group(1))
            values.append(number)
            details.append(match.group(0))

    clean = normalize_text(value)
    if re.fullmatch(r"[1-7]", clean):
        values.append(int(clean))
        details.append(clean)

    unique = sorted(set(values))
    return unique, " | ".join(dict.fromkeys(details))

def _risk_values_from_structural_cell(value: str) -> tuple[list[int], str]:
    """Risk kolonuna denk gelen hücredeki 1-7 değerlerini güvenli biçimde çıkarır.

    Bu yardımcı yalnızca yapısal olarak *Risk Değeri* kolonuyla eşleştirilmiş
    hücrelerde kullanılır. Bu nedenle genel metin ayrıştırıcısından daha dar ve
    daha güvenilirdir.
    """
    clean = normalize_text(value)
    if not clean:
        return [], ""

    # En yaygın ve en güvenilir biçimler: "2" ve "2/7".
    exact = re.fullmatch(r"\s*([1-7])\s*(?:/\s*7)?\s*", clean)
    if exact:
        return [int(exact.group(1))], clean

    compact = compact_for_match(clean)
    values: list[int] = []
    details: list[str] = []

    # A/B pay grubu gibi aynı risk hücresinde birden fazla değer bulunabilir.
    for match in re.finditer(r"(?<![\d/])([1-7])(?!\d)", compact):
        values.append(int(match.group(1)))
        details.append(match.group(0))

    return sorted(set(values)), " | ".join(dict.fromkeys(details))


def _positive_span(cell: Tag, attribute: str) -> int:
    """HTML rowspan/colspan değerini güvenli bir pozitif tam sayıya çevirir."""
    raw = normalize_text(cell.get(attribute, "1"))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(value, 50))


def _direct_table_rows(table: Tag) -> list[Tag]:
    """Yalnızca verilen tabloya ait satırları döndürür; iç içe tabloları dışlar."""
    rows: list[Tag] = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is table:
            rows.append(tr)
    return rows


def _direct_row_cells(row: Tag) -> list[Tag]:
    """Satırın kendi hücrelerini döndürür; hücre içindeki alt tabloları karıştırmaz."""
    cells = row.find_all(["th", "td"], recursive=False)
    if cells:
        return list(cells)
    # Bazı HTML üreticileri tbody/thead sarmalayıcılarını alışılmadık kurabilir.
    return [
        cell for cell in row.find_all(["th", "td"])
        if cell.find_parent("tr") is row
    ]


def _expanded_table_grid(table: Tag) -> list[list[tuple[str, Tag | None]]]:
    """rowspan/colspan uygulanmış gerçek görsel sütun ızgarasını oluşturur.

    Ham ``find_all(td)`` hücre sayısı, KAP tablolarında colspan/rowspan nedeniyle
    görsel sütun konumunu her zaman temsil etmez. Bu ızgara her hücreyi kapladığı
    gerçek sütunlara yayar; böylece Risk Değeri başlığı ile altındaki değer dikey
    olarak aynı koordinattan okunur.
    """
    grid: list[list[tuple[str, Tag | None]]] = []
    rows = _direct_table_rows(table)

    def ensure(row_index: int, col_index: int) -> None:
        while len(grid) <= row_index:
            grid.append([])
        while len(grid[row_index]) <= col_index:
            grid[row_index].append(("", None))

    for row_index, tr in enumerate(rows):
        ensure(row_index, 0)
        col_index = 0
        for cell in _direct_row_cells(tr):
            while True:
                ensure(row_index, col_index)
                if grid[row_index][col_index][1] is None:
                    break
                col_index += 1

            text = normalize_text(cell.get_text(" ", strip=True))
            colspan = _positive_span(cell, "colspan")
            rowspan = _positive_span(cell, "rowspan")
            for row_offset in range(rowspan):
                for col_offset in range(colspan):
                    target_row = row_index + row_offset
                    target_col = col_index + col_offset
                    ensure(target_row, target_col)
                    if grid[target_row][target_col][1] is None:
                        grid[target_row][target_col] = (text, cell)
            col_index += colspan

    width = max((len(row) for row in grid), default=0)
    for row in grid:
        while len(row) < width:
            row.append(("", None))
    return grid


def _is_exact_risk_header(value: str) -> bool:
    folded = fold_tr(value)
    return any(folded == fold_tr(label) for label in RISK_LABELS)


def _is_strategy_header(value: str) -> bool:
    folded = fold_tr(value)
    strategy_labels = (
        "Yatırım Stratejisi",
        "Fonun Yatırım Stratejisi",
        "Investment Strategy",
    )
    return any(folded == fold_tr(label) for label in strategy_labels)


def _single_risk_digit(value: str) -> list[int]:
    """Yalnızca tek başına yazılmış 1-7 rakamını kabul eder."""
    normalized = normalize_text(value)
    if re.fullmatch(r"[1-7]", normalized):
        return [int(normalized)]
    return []


def _extract_risk_from_table_columns(soup: BeautifulSoup) -> tuple[list[int], str]:
    """Risk başlığının yalnızca bir görsel satır altındaki aynı sütunu okur."""
    collected: list[int] = []
    evidence_parts: list[str] = []

    for table_number, table in enumerate(soup.find_all("table"), start=1):
        grid = _expanded_table_grid(table)
        if not grid:
            continue

        for header_index, header_row in enumerate(grid):
            data_index = header_index + 1
            if data_index >= len(grid):
                continue

            strategy_indexes = [
                index for index, (cell_text, _cell) in enumerate(header_row)
                if _is_strategy_header(cell_text)
            ]
            risk_indexes = [
                index for index, (cell_text, _cell) in enumerate(header_row)
                if _is_exact_risk_header(cell_text)
            ]
            if not strategy_indexes or not risk_indexes:
                continue

            data_row = grid[data_index]
            joined = " | ".join(cell_text for cell_text, _cell in data_row if normalize_text(cell_text))
            if joined and contains_any(joined, RISK_END_LABELS):
                continue

            for risk_index in risk_indexes:
                risk_header_cell = header_row[risk_index][1]
                left_strategy_indexes = [
                    index for index in strategy_indexes
                    if index < risk_index and header_row[index][1] is not risk_header_cell
                ]
                if not left_strategy_indexes:
                    continue
                strategy_index = max(left_strategy_indexes)
                strategy_header_cell = header_row[strategy_index][1]
                if risk_index >= len(data_row) or strategy_index >= len(data_row):
                    continue

                strategy_text, strategy_cell = data_row[strategy_index]
                risk_text, risk_cell = data_row[risk_index]
                if risk_cell is None or risk_cell is risk_header_cell:
                    continue
                if strategy_cell is None or strategy_cell is strategy_header_cell:
                    continue
                if risk_cell is strategy_cell:
                    continue
                if not normalize_text(strategy_text):
                    continue

                values = _single_risk_digit(risk_text)
                if not values:
                    continue

                collected.extend(values)
                evidence_parts.append(
                    f"TABLO {table_number} | DİKEY GÖRSEL SÜTUN {risk_index + 1} | "
                    f"BAŞLIK SATIRI: {normalize_text(strategy_header_cell.get_text(' ', strip=True))} | "
                    f"{normalize_text(risk_header_cell.get_text(' ', strip=True))} | "
                    f"HEMEN ALT SATIR SOL METİN: {truncate(strategy_text, 700)} | "
                    f"HEMEN ALT SATIR RİSK HÜCRESİ: {risk_text}"
                )

    return sorted(set(collected)), " || ".join(dict.fromkeys(evidence_parts))


def _extract_risk_from_horizontal_row_pair(soup: BeautifulSoup) -> tuple[list[int], str]:
    """Başlık satırı ve hemen altındaki veri satırını yatay DOM sırasıyla doğrular.

    Bu kontrol görsel sütun ızgarasından bağımsız olarak doğrudan iki komşu
    ``tr`` satırındaki ayrı hücre sırasını inceler. ALC biçiminde başlıklar
    ``Yatırım Stratejisi | Risk Değeri`` ve alt satır ``uzun metin | 6`` olur.
    """
    values: list[int] = []
    evidence: list[str] = []
    for table_number, table in enumerate(soup.find_all("table"), start=1):
        rows = _direct_table_rows(table)
        for row_index in range(len(rows) - 1):
            header_cells = _direct_row_cells(rows[row_index])
            data_cells = _direct_row_cells(rows[row_index + 1])
            if len(header_cells) < 2 or len(data_cells) < 2:
                continue
            header_texts = [normalize_text(cell.get_text(" ", strip=True)) for cell in header_cells]
            strategy_positions = [i for i, cell_text in enumerate(header_texts) if _is_strategy_header(cell_text)]
            risk_positions = [i for i, cell_text in enumerate(header_texts) if _is_exact_risk_header(cell_text)]
            if not strategy_positions or not risk_positions:
                continue
            strategy_pos = strategy_positions[-1]
            risk_pos = risk_positions[0]
            if strategy_pos >= risk_pos:
                continue

            # KAP'ın ALC yapısında başlık ve veri satırları iki ayrı hücreden oluşur.
            # Colspan değerleri hücre sayısını değiştirmez; DOM sırası soldan sağadır.
            strategy_data = normalize_text(data_cells[0].get_text(" ", strip=True))
            risk_data = normalize_text(data_cells[-1].get_text(" ", strip=True))
            if not strategy_data or data_cells[0] is data_cells[-1]:
                continue
            found = _single_risk_digit(risk_data)
            if not found:
                continue
            values.extend(found)
            evidence.append(
                f"TABLO {table_number} | YATAY İKİ SATIR | "
                f"BAŞLIKLAR: {' | '.join(header_texts)} | "
                f"ALT SATIR: {truncate(strategy_data, 700)} | {risk_data}"
            )
    return sorted(set(values)), " || ".join(dict.fromkeys(evidence))


def _direct_block_children(tag: Tag) -> list[Tag]:
    return [child for child in tag.find_all(recursive=False) if isinstance(child, Tag)]


def _extract_risk_from_div_grid_pairs(soup: BeautifulSoup) -> tuple[list[int], str]:
    """Table yerine div/grid/flex kullanılan iki satırlı KAP bloklarını çözer."""
    values: list[int] = []
    evidence: list[str] = []
    for parent in soup.find_all(["div", "section", "article"]):
        header_children = _direct_block_children(parent)
        if len(header_children) < 2 or len(header_children) > 12:
            continue
        header_texts = [normalize_text(child.get_text(" ", strip=True)) for child in header_children]
        strategy_positions = [i for i, cell_text in enumerate(header_texts) if _is_strategy_header(cell_text)]
        risk_positions = [i for i, cell_text in enumerate(header_texts) if _is_exact_risk_header(cell_text)]
        if not strategy_positions or not risk_positions:
            continue
        strategy_pos = strategy_positions[-1]
        risk_pos = risk_positions[0]
        if strategy_pos >= risk_pos:
            continue

        sibling = parent.find_next_sibling()
        while sibling is not None and not isinstance(sibling, Tag):
            sibling = sibling.find_next_sibling()
        if sibling is None:
            continue
        data_children = _direct_block_children(sibling)
        if len(data_children) <= risk_pos or len(data_children) <= strategy_pos:
            continue
        strategy_data = normalize_text(data_children[strategy_pos].get_text(" ", strip=True))
        risk_data = normalize_text(data_children[risk_pos].get_text(" ", strip=True))
        if not strategy_data:
            continue
        found = _single_risk_digit(risk_data)
        if not found:
            continue
        values.extend(found)
        evidence.append(
            f"DIV/GRID YATAY+DİKEY | BAŞLIKLAR: {' | '.join(header_texts)} | "
            f"HEMEN ALT BLOK: {truncate(strategy_data, 700)} | {risk_data}"
        )
    return sorted(set(values)), " || ".join(dict.fromkeys(evidence))


def _risk_section(lines: Sequence[str]) -> list[str]:
    start_index = -1
    for index, line in enumerate(lines):
        if contains_any(line, RISK_SECTION_LABELS):
            start_index = index
            break
    if start_index < 0:
        return []
    section: list[str] = []
    for line in lines[start_index:start_index + 180]:
        if section and contains_any(line, RISK_END_LABELS):
            break
        normalized = normalize_text(line)
        if normalized:
            section.append(normalized)
    return section


def _extract_risk_from_wide_section(lines: Sequence[str]) -> tuple[list[int], str]:
    """Sınırlandırılmış geniş risk bölümündeki adayları toplar.

    Strateji içindeki rastgele 1-7 değerleri taranmaz. Yalnız açık risk etiketi,
    tek başına risk düğümü ve ALC'deki gibi bölümün sonunda metne birleşmiş tek
    bağımsız 1-7 değeri değerlendirilir.
    """
    section = _risk_section(lines)
    if not section:
        return [], ""
    if not any(_is_strategy_header(line) or "yatırım stratejisi" in normalize_text(line).casefold() for line in section[:12]):
        return [], ""
    if not any(contains_any(line, RISK_LABELS) for line in section[:12]):
        return [], ""

    joined = " | ".join(section)
    found, detail = _risk_values_from_text(joined)
    evidence: list[str] = []
    if detail:
        evidence.append(f"GENİŞ BÖLÜM AÇIK ETİKET: {detail}")

    # ALC canlı sayfası: son strateji metni DOM'da risk hücresindeki 6 ile
    # ``... dahil edilmeyecektir 6`` biçiminde düzleşebilir.
    tail = normalize_text(section[-1])
    match = re.search(r"([1-7])\s*$", tail)
    if match:
        digit_start = match.start(1)
        prefix = tail[:digit_start]
        previous = prefix[-1:] if prefix else ""
        decimal_or_multidigit = bool(re.search(r"\d[.,]\s*$", prefix)) or bool(previous.isdigit())
        disallowed_marker = previous in {"%", "/", "+", "-", "−"}
        if len(prefix.strip()) >= 40 and not decimal_or_multidigit and not disallowed_marker:
            found.append(int(match.group(1)))
            evidence.append(f"GENİŞ BÖLÜM SON BAĞIMSIZ RİSK: {truncate(tail, 1200)}")

    return sorted(set(value for value in found if 1 <= value <= 7)), " || ".join(evidence)


def _extract_risk_from_section_segment(lines: Sequence[str]) -> tuple[list[int], str]:
    """Risk bölümünün son veri segmentindeki değeri ayrı yedek yöntemle çıkarır."""
    section = _risk_section(lines)
    if not section or not any(contains_any(line, RISK_LABELS) for line in section[:12]):
        return [], ""

    header_position = 0
    for index, line in enumerate(section):
        if contains_any(line, RISK_LABELS):
            header_position = index
            break
    content = [line for line in section[header_position + 1:] if normalize_text(line)]
    if not content:
        return [], ""

    tail = normalize_text(content[-1])
    if re.fullmatch(r"[1-7]", tail):
        return [int(tail)], f"RİSK BÖLÜMÜ SON HÜCRE: {tail}"

    match = re.search(r"([1-7])\s*$", tail)
    if match:
        digit_start = match.start(1)
        prefix = tail[:digit_start]
        previous = prefix[-1:] if prefix else ""
        decimal_or_multidigit = bool(re.search(r"\d[.,]\s*$", prefix)) or bool(previous.isdigit())
        disallowed_marker = previous in {"%", "/", "+", "-", "−"}
        if len(prefix.strip()) >= 40 and not decimal_or_multidigit and not disallowed_marker:
            value = int(match.group(1))
            return [value], f"RİSK BÖLÜMÜ SON SEGMENT: {truncate(tail, 1200)}"
    return [], ""

def extract_risk(soup: BeautifulSoup, lines: Sequence[str]) -> ParseValue:
    """A-E bağımsız HTML yöntemlerini çalıştırıp risk adaylarını çapraz doğrular."""
    evidence_parts: list[str] = []

    vertical_values, vertical_evidence = _extract_risk_from_table_columns(soup)
    horizontal_values, horizontal_evidence = _extract_risk_from_horizontal_row_pair(soup)
    div_values, div_evidence = _extract_risk_from_div_grid_pairs(soup)
    wide_values, wide_evidence = _extract_risk_from_wide_section(lines)
    segment_values, segment_evidence = _extract_risk_from_section_segment(lines)

    for evidence in (
        vertical_evidence, horizontal_evidence, div_evidence,
        wide_evidence, segment_evidence,
    ):
        if evidence:
            evidence_parts.append(evidence)

    # Açık etiket/bağlam kuralı ayrıca tablo satırı, bölüm ve yakın DOM üzerinde
    # çalışır. Para birimi ve pay grubu yakınlığı v9.4'te kesinlikle kullanılmaz.
    text_values: list[int] = []
    rows = table_rows_with_empty(soup)
    for index, row in enumerate(rows):
        joined = " | ".join(row)
        if not contains_any(joined, RISK_LABELS):
            continue
        window = rows[index:index + 2]  # yalnız başlık ve hemen alt satır
        evidence = " || ".join(" | ".join(item) for item in window)
        values, detail = _risk_values_from_text(evidence)
        if detail:
            evidence_parts.append(f"AÇIK ETİKET TABLO: {detail}")
        text_values.extend(values)

    section = _risk_section(lines)
    if section:
        values, detail = _risk_values_from_text(" | ".join(section))
        if detail:
            evidence_parts.append(f"AÇIK ETİKET BÖLÜM: {detail}")
        text_values.extend(values)

    text_values = sorted(set(value for value in text_values if 1 <= value <= 7))

    methods = {
        "DİKEY_TABLO": vertical_values,
        "YATAY_TABLO": horizontal_values,
        "DIV_GRID": div_values,
        "AÇIK_ETİKET": text_values,
        "GENİŞ_BÖLÜM": wide_values,
        "BÖLÜM_SEGMENTİ": segment_values,
    }
    active = {name: sorted(set(values)) for name, values in methods.items() if values}

    structural = {
        name: values for name, values in active.items()
        if name in {"DİKEY_TABLO", "YATAY_TABLO", "DIV_GRID"}
    }
    structural_union = sorted(set(value for values in structural.values() for value in values))
    if len(structural_union) > 1:
        return ParseValue(
            value="—",
            raw_value=", ".join(map(str, structural_union)),
            source_label="KAP_DETAIL_HTML:Risk Değeri [YAPISAL ÇELİŞKİ]",
            evidence=truncate(" || ".join(dict.fromkeys(evidence_parts)), 2400),
            confidence="YOK",
            matched_scope="YAPISAL_CELISKI",
            decision_reason=f"Yatay/dikey/div yapısal kontroller çelişti: {structural}.",
            conflict_flag="EVET",
        )

    if structural_union:
        values = structural_union
        supporting = [name for name, candidates in active.items() if set(candidates).intersection(values)]
        confidence = "ÇOK YÜKSEK" if len(supporting) >= 2 else "YÜKSEK"
        source = "KAP_DETAIL_HTML:Risk Değeri [YATAY+DİKEY YAPISAL]"
        reason = "Risk başlık satırı ve hemen altındaki veri satırı yapısal olarak okundu"
        if len(supporting) >= 2:
            reason += "; " + " + ".join(supporting) + " aynı değeri doğruladı"
        reason += "."
        scope = "YAPISAL_YATAY_DIKEY"
    elif text_values:
        values = text_values
        supporting = [name for name, candidates in active.items() if set(candidates).intersection(values)]
        confidence = "ÇOK YÜKSEK" if len(supporting) >= 2 else "YÜKSEK"
        source = "KAP_DETAIL_HTML:Risk Değeri [AÇIK ETİKET]"
        reason = "Açık Risk Değeri etiketiyle 1-7 değeri bulundu."
        scope = "ACIK_ETIKET"
    elif wide_values and segment_values and set(wide_values).intersection(segment_values):
        values = sorted(set(wide_values).intersection(segment_values))
        confidence = "ÇOK YÜKSEK"
        source = "KAP_DETAIL_HTML:Risk Değeri [GENİŞ BÖLÜM + SON SEGMENT]"
        reason = "ALC tipi geniş bölüm sonu değeri iki bağımsız metin yöntemiyle doğrulandı."
        scope = "GENIS_BOLUM_DOGRULANMIS"
    elif wide_values:
        values = wide_values
        confidence = "YÜKSEK"
        source = "KAP_DETAIL_HTML:Risk Değeri [GENİŞ BÖLÜM]"
        reason = "Sınırlandırılmış risk bölümünün sonundaki bağımsız 1-7 değeri bulundu."
        scope = "GENIS_BOLUM"
    elif segment_values:
        values = segment_values
        confidence = "ORTA"
        source = "KAP_DETAIL_HTML:Risk Değeri [BÖLÜM SEGMENTİ]"
        reason = "Risk bölümünün son veri segmentindeki bağımsız 1-7 değeri kullanıldı."
        scope = "BOLUM_SEGMENTI"
    else:
        return ParseValue(
            value="—",
            raw_value="",
            source_label="KAP_DETAIL_HTML:Risk Değeri",
            evidence=truncate(" || ".join(dict.fromkeys(evidence_parts)), 2400)
            or "İlgili görünür KAP bölümünde güvenilir 1-7 risk değeri bulunamadı.",
            confidence="YOK",
            decision_reason="Yatay, dikey, div/grid, açık etiket, geniş bölüm ve son segment yöntemleri sonuç üretmedi.",
        )

    values = sorted(set(value for value in values if 1 <= value <= 7))
    selected = max(values)
    detail = ", ".join(str(item) for item in values)
    return ParseValue(
        value=str(selected),
        raw_value=detail,
        source_label=source,
        evidence=truncate(" || ".join(dict.fromkeys(evidence_parts)), 2400),
        confidence=confidence,
        matched_scope=scope,
        decision_reason=(
            reason if len(values) == 1
            else reason + f" Çoklu değerler ({detail}) içinde ana risk olarak en yüksek değer {selected} seçildi."
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

    risk_samples = [
        (
            "ALC_DIKEY_KOLON_COLSPAN",
            """<html><body><table>
            <tr><th colspan="5">Fonun Yatırım Stratejisi ve Risk Değeri</th></tr>
            <tr><th colspan="4">Yatırım Stratejisi</th><th>Risk Değeri</th></tr>
            <tr><td colspan="4"><div>Uzun yatırım stratejisi metni ve içinde %80, 2.4 gibi rakamlar vardır.</div></td><td><span>6</span></td></tr>
            </table><div>Fon Karşılaştırma Ölçütü</div></body></html>""",
            "6",
        ),
        (
            "ANL_DIKEY_KOLON_ROWSPAN",
            """<html><body><table>
            <tr><th>Yatırım Stratejisi</th><th rowspan="1">Risk Değeri</th></tr>
            <tr><td>Strateji metni sağdaki risk değerinden ayrı hücrededir.</td><td>1</td></tr>
            </table><div>Fon Karşılaştırma Ölçütü</div></body></html>""",
            "1",
        ),
        (
            "DIKEY_KOLON_SADECE_RAKAM",
            """<html><body><table>
            <tr><th>Yatırım Stratejisi</th><th>Risk Değeri</th></tr>
            <tr><td>Strateji metni</td><td>Risk 5</td></tr>
            </table><div>Fon Karşılaştırma Ölçütü</div></body></html>""",
            "—",
        ),
    ]

    for name, html, expected in risk_samples:
        soup = clean_soup(html)
        result = extract_risk(soup, visible_lines(soup))
        if result.value == expected:
            passed += 1
        else:
            failures.append(
                f"{name}: beklenen={expected}, bulunan={result.value}, neden={result.decision_reason}"
            )

    if failures:
        raise RuntimeError("Kural öz testi başarısız: " + " || ".join(failures))
    return passed, len(samples) + len(risk_samples)


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
    reader = PdfReader(BytesIO(content))
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
    sep = r"\s*[:：]?\s*"
    patterns = (
        # Yatırımcı Bilgi Formu'ndaki resmî ana etiket önce değerlendirilir.
        # Hem "İhraç Tarihi: 06/11/2006" hem "İhraç Tarihi:06/11/2006" desteklenir.
        ("İhraç tarihi", rf"İhraç\s*Tarihi{sep}{date_expr}"),
        ("İlk ihraç tarihi", rf"İlk\s+İhraç\s+Tarihi{sep}{date_expr}"),
        ("Fonun halka arz tarihi", rf"Fon[’'`]?un\s+Halka\s+Arz\s+Tarihi{sep}{date_expr}"),
        ("Halka arz tarihi", rf"Halka\s+Arz\s+Tarihi{sep}{date_expr}"),
        ("Fonun satış başlangıç tarihi", rf"Fon[’'`]?un\s+Satış\s+Başlangıç\s+Tarihi{sep}{date_expr}"),
        ("Satış başlangıç tarihi", rf"Satış\s+Başlangıç\s+Tarihi{sep}{date_expr}"),
        (
            "Fon paylarının satış başlangıç tarihi",
            rf"Fon\s+paylarının\s+satışına\s+{date_expr}\s+tarihinde\s+başlanmıştır",
        ),
        ("Fonun başlangıç tarihi", rf"Fon[’'`]?un\s+Başlangıç\s+Tarihi{sep}{date_expr}"),
        ("Fonun kuruluş tarihi", rf"Fon[’'`]?un\s+Kuruluş\s+Tarihi{sep}{date_expr}"),
        ("Kuruluş tarihi", rf"Kuruluş\s+Tarihi{sep}{date_expr}"),
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
            decision_reason="PDF içindeki resmî tarih etiketi ve gg/mm/yyyy değeri eşleştirildi.",
        )

    return ParseValue(
        value="—",
        raw_value="",
        source_label="KAP_YBF_PDF:Başlangıç",
        evidence="Yatırımcı Bilgi Formu içinde İhraç Tarihi veya diğer başlangıç tarihleri bulunamadı.",
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
                    pdf_content = pdf_response.content
                    content_type = normalize_text(pdf_response.headers.get("Content-Type")).casefold()
                    if not pdf_content.startswith(b"%PDF-"):
                        raise RuntimeError(
                            "YBF_PDF_DEGIL: KAP bağlantısı PDF yerine farklı içerik döndürdü "
                            f"(Content-Type={content_type or 'bilinmiyor'}, ilk baytlar={pdf_content[:12]!r})."
                        )
                    pdf_text = pdf_text_from_bytes(pdf_content)

                    # Fallback teşhisi için gerçek PDF ve ayrıştırılmış metin saklanır.
                    save_investor_form(fund, pdf_content, pdf_text)

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
        if should_save:
            save_html(fund, html)

        db_start = normalize_text(db.get("start_year"))
        db_risk = normalize_text(db.get("risk_level"))
        db_trade = normalize_text(db.get("transaction_status"))
        tefas_row = tefas_traded_funds.get(fund.fund_code, {})

        parse_method = f"{SCRIPT_VERSION} | KAP_LIST_JSON + KAP_DETAIL_VISIBLE_HTML + TEFAS_TRADED_LIST_JSON"
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
# V9 İLERLEME / TEŞHİS / DEVAM KATMANI
# -----------------------------------------------------------------------------

def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)


def load_progress() -> dict[str, FundResult]:
    if not PROGRESS_PATH.exists():
        return {}
    try:
        payload = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        rows = payload.get("results", {}) if isinstance(payload, dict) else {}
        if isinstance(rows, list):
            rows = {normalize_text(row.get("fund_code")).upper(): row for row in rows if isinstance(row, dict)}
        result: dict[str, FundResult] = {}
        allowed = set(FundResult.__annotations__)
        for code, row in rows.items():
            if not isinstance(row, dict):
                continue
            clean = {key: row.get(key) for key in allowed}
            missing = allowed - set(clean)
            if missing:
                continue
            try:
                item = FundResult(**clean)
            except Exception:
                continue
            normalized_code = normalize_text(code or item.fund_code).upper()
            if normalized_code:
                result[normalized_code] = item
        return result
    except Exception as exc:
        print(f"UYARI: İlerleme dosyası okunamadı; boş başlanacak: {type(exc).__name__}: {exc}")
        return {}


def retry_needed(result: FundResult | None) -> bool:
    if result is None:
        return True
    return bool(
        result.error
        or result.http_status != 200
        or result.page_code_verified != "EVET"
        or result.transaction_status == "BİLİNMİYOR"
        or result.start_year in {"", "—"}
        or result.risk_level in {"", "—"}
    )


def merge_results(previous: FundResult | None, current: FundResult) -> FundResult:
    """Yeni deneme kötüleşirse eski doğru alanları korur; eksik alanları yeni sonuçla tamamlar."""
    if previous is None:
        return current

    # Geçici HTTP/ağ hatası, daha önce alınmış HTTP 200 kaydını asla ezmez.
    if current.error or current.http_status != 200 or current.page_code_verified != "EVET":
        if previous.http_status == 200 and previous.page_code_verified == "EVET":
            return previous
        return current

    merged = asdict(current)
    old = asdict(previous)

    if current.start_year in {"", "—"} and previous.start_year not in {"", "—"}:
        for key in ("start_date", "start_year", "start_source", "start_evidence", "start_confidence"):
            merged[key] = old[key]

    if current.risk_level in {"", "—"} and previous.risk_level not in {"", "—"}:
        for key in (
            "risk_level", "risk_detail", "risk_multi_value", "risk_source",
            "risk_evidence", "risk_confidence",
        ):
            merged[key] = old[key]

    if current.transaction_status == "BİLİNMİYOR" and previous.transaction_status in {"AÇIK", "KAPALI"}:
        for key in (
            "transaction_status", "transaction_source", "transaction_evidence",
            "transaction_confidence", "transaction_matched_pattern",
            "transaction_matched_scope", "transaction_decision_reason",
            "transaction_conflict_flag", "tefas_traded_list_match",
            "tefas_traded_list_status", "tefas_traded_list_title",
            "tefas_traded_list_date",
        ):
            merged[key] = old[key]

    # Birleştirilmiş kayıt teknik olarak sağlamsa geçici hata metni tutulmaz.
    if merged["http_status"] == 200 and merged["page_code_verified"] == "EVET":
        merged["error"] = ""
    return FundResult(**merged)


def append_attempt_event(result: FundResult, *, label: str) -> None:
    ATTEMPT_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "fund_code": result.fund_code,
        "http_status": result.http_status,
        "category": failure_category(result),
        "start_year": result.start_year,
        "risk_level": result.risk_level,
        "transaction_status": result.transaction_status,
        "error": result.error,
    }
    with ATTEMPT_EVENTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def failure_category(result: FundResult) -> str:
    error = normalize_text(result.error)
    upper = error.upper()
    for status in (429, 403, 404, 408, 500, 502, 503, 504):
        if f"HTTP_{status}" in upper or f"{status} CLIENT ERROR" in upper or f"{status} SERVER ERROR" in upper:
            return f"HTTP_{status}"
    if "TIMEOUT" in upper or "READTIMEDOUT" in upper or "CONNECTTIMEOUT" in upper:
        return "TIMEOUT"
    if "CONNECTION" in upper or "REMOTEDISCONNECTED" in upper or "CONNECTIONRESET" in upper:
        return "CONNECTION_ERROR"
    if "SSL" in upper:
        return "SSL_ERROR"
    if result.http_status not in (None, 200):
        return f"HTTP_{result.http_status}"
    if result.page_code_verified != "EVET":
        return "PAGE_CODE_MISMATCH"
    if result.transaction_status == "BİLİNMİYOR":
        return "DOM_PARSE_UNKNOWN"
    missing_start = result.start_year in {"", "—"}
    missing_risk = result.risk_level in {"", "—"}
    if missing_start and missing_risk:
        return "MISSING_START_AND_RISK"
    if missing_start:
        return "MISSING_START"
    if missing_risk:
        return "MISSING_RISK"
    if error:
        return "OTHER_ERROR"
    return "OK"


def save_progress_and_diagnostics(
    progress: dict[str, FundResult],
    *,
    selected_codes: Sequence[str],
    mode: str,
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    serialized = {code: asdict(item) for code, item in sorted(progress.items())}
    atomic_write_json(PROGRESS_PATH, {
        "schema_version": 1,
        "script_version": SCRIPT_VERSION,
        "updated_at": now,
        "saved_result_count": len(serialized),
        "results": serialized,
    })

    selected_set = {normalize_text(code).upper() for code in selected_codes if normalize_text(code)}
    selected_results = [progress[code] for code in sorted(selected_set) if code in progress]
    failures = [item for item in selected_results if retry_needed(item)]
    failure_rows = []
    category_counts: dict[str, int] = {}
    for item in failures:
        category = failure_category(item)
        category_counts[category] = category_counts.get(category, 0) + 1
        failure_rows.append({
            "fund_code": item.fund_code,
            "fund_name": item.fund_name,
            "category": category,
            "http_status": item.http_status,
            "page_code_verified": item.page_code_verified,
            "transaction_status": item.transaction_status,
            "detail_url": item.detail_url,
            "final_url": item.final_url,
            "error": item.error,
            "decision_reason": item.transaction_decision_reason,
        })

    atomic_write_json(FAILED_CODES_PATH, {
        "script_version": SCRIPT_VERSION,
        "updated_at": now,
        "count": len(failures),
        "codes": [item.fund_code for item in failures],
    })
    atomic_write_json(REQUEST_FAILURES_PATH, {
        "script_version": SCRIPT_VERSION,
        "updated_at": now,
        "count": len(failure_rows),
        "category_counts": dict(sorted(category_counts.items())),
        "failures": failure_rows,
    })

    completed_valid = sum(1 for item in selected_results if not retry_needed(item))
    status = "COMPLETE" if selected_set and len(selected_results) == len(selected_set) and not failures else "IN_PROGRESS"
    atomic_write_json(RUN_STATE_PATH, {
        "script_version": SCRIPT_VERSION,
        "updated_at": now,
        "mode": mode,
        "status": status,
        "selected_count": len(selected_set),
        "selected_saved_count": len(selected_results),
        "selected_valid_count": completed_valid,
        "selected_unresolved_count": len(failures),
        "total_saved_across_runs": len(progress),
        "next_action": (
            "Program kontrollü tekrarları tamamladı. Aynı modu yeniden çalıştırırsanız yalnızca hata veya eksik alanlar tekrar denenir."
            if failures else
            "İlk 300 tamamlandıysa menü 4 ile tüm KAP YF/Y fonlarına devam edebilirsiniz."
        ),
    })


def reset_progress_files() -> None:
    for path in (PROGRESS_PATH, FAILED_CODES_PATH, REQUEST_FAILURES_PATH, RUN_STATE_PATH, ATTEMPT_EVENTS_PATH):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


def run_fund_batch(
    funds_to_run: Sequence[FundEntry],
    *,
    progress: dict[str, FundResult],
    selected_codes: Sequence[str],
    mode: str,
    db_values: dict[str, dict[str, str]],
    tefas_traded_funds: dict[str, dict[str, str]],
    tefas_list_error: str,
    workers: int,
    delay: float,
    save_raw_html: bool,
    label: str,
) -> None:
    if not funds_to_run:
        print(f"{label}: çalıştırılacak fon yok.")
        save_progress_and_diagnostics(progress, selected_codes=selected_codes, mode=mode)
        return

    print(f"\n{label}: {len(funds_to_run)} fon | İşçi {workers} | Aralık {delay:.2f} sn")
    rate_limiter = GlobalRateLimiter(
        delay,
        routine_request_limit=ROUTINE_REQUEST_LIMIT,
        routine_cooldown_seconds=ROUTINE_COOLDOWN_SECONDS,
    )
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
            for fund in funds_to_run
        }
        completed = 0
        for future in as_completed(future_map):
            completed += 1
            result = future.result()
            append_attempt_event(result, label=label)
            merged_result = merge_results(progress.get(result.fund_code), result)
            progress[result.fund_code] = merged_result
            print_result_line(completed, len(funds_to_run), result)
            if merged_result is not result and result.error:
                print("       Önceki başarılı/eksik kayıt korundu; geçici hata verinin üzerine yazılmadı.")
            # Her fon sonrası atomik kayıt: elektrik/internet/terminal kapanmasında veri kaybolmaz.
            save_progress_and_diagnostics(progress, selected_codes=selected_codes, mode=mode)


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
        f"KAP YAT DETAY + TEFAS İŞLEM LİSTESİ DOĞRULAMA TESTİ {SCRIPT_VERSION}",
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
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Kaydedilmiş başarılı sonuçları yükler ve yalnızca eksik/başarısız kodları çalıştırır.",
    )
    parser.add_argument(
        "--retry-only",
        action="store_true",
        help="Yalnızca kaydedilmiş başarısız veya belirsiz fonları yeniden dener.",
    )
    parser.add_argument(
        "--retry-rounds",
        type=int,
        default=DEFAULT_RETRY_ROUNDS,
        help=f"Normal taramadan sonra yavaş otomatik tekrar turu. Varsayılan: {DEFAULT_RETRY_ROUNDS}",
    )
    parser.add_argument(
        "--reset-progress",
        action="store_true",
        help="Kaydedilmiş ilerleme ve teşhis dosyalarını silerek sıfırdan başlar.",
    )
    return parser.parse_args()


def interactive_selection() -> tuple[bool, list[str], int, bool, bool]:
    print("\nÇALIŞMA MODU")
    print(f"1 - Hızlı doğrulama testi ({len(DEFAULT_CODES)} fon: {', '.join(DEFAULT_CODES)})")
    print("2 - Belirli fon kodları")
    print(f"3 - İlk {DEFAULT_DIAGNOSTIC_LIMIT} KAP YF/Y fonu — teşhis + kaydet + devam desteği")
    print("4 - Tüm KAP YF/Y fonları — kayıtlı ilerlemeden devam")
    print("5 - Yalnızca daha önce başarısız/belirsiz olan fonları tekrar dene")
    choice = input("Seçim [3]: ").strip() or "3"

    if choice == "4":
        confirmation = input(
            "Tüm KAP YF/Y fonları taranacak ve kayıtlı ilerleme kullanılacak. Devam için ALL yazın: "
        ).strip().upper()
        if confirmation != "ALL":
            print(f"Tüm tarama iptal edildi; ilk {DEFAULT_DIAGNOSTIC_LIMIT} fon seçildi.")
            return True, [], DEFAULT_DIAGNOSTIC_LIMIT, True, False
        return True, [], 0, True, False

    if choice == "5":
        return True, [], 0, True, True

    if choice == "2":
        entered = input("Fon kodlarını virgülle girin: ").strip()
        codes = [normalize_text(item).upper() for item in entered.split(",") if normalize_text(item)]
        return False, list(dict.fromkeys(codes)) or DEFAULT_CODES.copy(), 0, False, False

    if choice == "1":
        return False, DEFAULT_CODES.copy(), 0, False, False

    return True, [], DEFAULT_DIAGNOSTIC_LIMIT, True, False


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
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)

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
    selected_limit = max(0, args.limit)
    resume_mode = bool(args.resume)
    retry_only_mode = bool(args.retry_only)

    if not scan_all and not codes:
        if sys.stdin.isatty():
            scan_all, codes, selected_limit, resume_mode, retry_only_mode = interactive_selection()
        else:
            codes = DEFAULT_CODES.copy()

    selected, missing_codes = select_funds(
        funds,
        scan_all=scan_all,
        codes=codes,
        limit=selected_limit,
    )

    if missing_codes:
        print("KAP aktif YF listesinde bulunamayan kodlar: " + ", ".join(missing_codes))
    if not selected:
        raise RuntimeError("Test edilecek geçerli fon bulunamadı.")

    print(f"Test edilecek fon: {len(selected)}")
    print(f"İşçi sayısı       : {max(1, min(args.workers, 4))}")
    print(f"İstek aralığı     : {max(0.0, args.delay):.2f} saniye")
    print(f"Düzenli mola      : Her {ROUTINE_REQUEST_LIMIT} KAP isteğinde {ROUTINE_COOLDOWN_SECONDS} saniye")
    print("429 koruması      : 180 sn → 600 sn → 1200 sn; ardından otomatik devam")

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

    workers = max(1, min(int(args.workers), 4))
    save_raw_html = bool(args.save_html or len(selected) <= 20)
    mode = (
        "RETRY_ONLY" if retry_only_mode else
        (f"FIRST_{selected_limit}" if scan_all and selected_limit else ("ALL" if scan_all else "CODES"))
    )

    if args.reset_progress:
        reset_progress_files()
        print("Kaydedilmiş ilerleme sıfırlandı.")

    progress = load_progress()
    if progress:
        print(f"Kayıtlı ilerleme  : {len(progress)} fon sonucu yüklendi")

    selected_codes = [item.fund_code for item in selected]
    selected_by_code = {item.fund_code: item for item in selected}

    if retry_only_mode:
        target_funds = [
            fund for fund in selected
            if fund.fund_code in progress and retry_needed(progress.get(fund.fund_code))
        ]
        if not progress:
            print("UYARI: Retry-only için kayıtlı ilerleme yok; normal seçili taramaya geçiliyor.")
            target_funds = list(selected)
    elif resume_mode:
        target_funds = [fund for fund in selected if retry_needed(progress.get(fund.fund_code))]
    else:
        target_funds = list(selected)

    skipped_valid = len(selected) - len(target_funds)
    if skipped_valid > 0:
        print(f"Geçerli kayıt atlandı: {skipped_valid}")
    print(f"Bu çalışmada hedef: {len(target_funds)}")

    started = time.perf_counter()
    run_fund_batch(
        target_funds,
        progress=progress,
        selected_codes=selected_codes,
        mode=mode,
        db_values=db_values,
        tefas_traded_funds=tefas_traded_funds,
        tefas_list_error=tefas_list_error,
        workers=workers,
        delay=max(0.0, args.delay),
        save_raw_html=save_raw_html,
        label="NORMAL TARAMA",
    )

    retry_rounds = max(0, min(int(args.retry_rounds), 6))
    for retry_round in range(1, retry_rounds + 1):
        unresolved_funds = [
            selected_by_code[code]
            for code in selected_codes
            if code in selected_by_code and retry_needed(progress.get(code))
        ]
        if not unresolved_funds:
            break
        slow_delay = max(SLOW_RETRY_DELAY_SECONDS * retry_round, max(0.0, args.delay) * 2.0)
        run_fund_batch(
            unresolved_funds,
            progress=progress,
            selected_codes=selected_codes,
            mode=mode,
            db_values=db_values,
            tefas_traded_funds=tefas_traded_funds,
            tefas_list_error=tefas_list_error,
            workers=1,
            delay=slow_delay,
            save_raw_html=True,
            label=f"YAVAŞ OTOMATİK TEKRAR {retry_round}/{retry_rounds}",
        )

    results = [progress[code] for code in selected_codes if code in progress]
    results.sort(key=lambda item: item.fund_code)
    elapsed_seconds = time.perf_counter() - started
    save_progress_and_diagnostics(progress, selected_codes=selected_codes, mode=mode)

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
    print(f"İLERLEME     : {PROGRESS_PATH}")
    print(f"BAŞARISIZLAR : {FAILED_CODES_PATH}")
    print(f"TEŞHİS       : {REQUEST_FAILURES_PATH}")
    print(f"DURUM        : {RUN_STATE_PATH}")
    print(f"DENEME GÜNLÜĞÜ: {ATTEMPT_EVENTS_PATH}")
    unresolved_count = sum(1 for item in results if retry_needed(item))
    if unresolved_count:
        print(f"\nSONUÇ: {unresolved_count} fon hâlâ başarısız/belirsiz. Aynı modu tekrar çalıştırabilirsiniz.")
    else:
        print("\nSONUÇ: Seçilen fonların tamamı geçerli şekilde tamamlandı.")
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
