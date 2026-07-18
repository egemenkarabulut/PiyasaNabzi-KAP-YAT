#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""TEFAS 60 aylık fiyat serisinden güvenli başlangıç tarihi yedeği.

Bu modül yalnız KAP HTML ve KAP Yatırımcı Bilgi Formu PDF kaynaklarında
başlangıç tarihi bulunamayan fonlar için çalışır.

Kesin kural:
- ``resultList`` içindeki en eski geçerli tarih esas alınır.
- İlk kaydın fiyatı ``0`` olsa bile tarih geçerlidir.
- Fiyat yalnız teşhis amacıyla saklanır; tarih kabulünü engellemez.
- En eski tarih 60 aylık doğal sınırın 20 gün çevresindeyse seri kırpılmış
  kabul edilir ve başlangıç yılı üretilmez.
- Fon başına yalnız bir POST isteği gönderilir; bu modül kendi içinde aynı
  isteği otomatik tekrar etmez.
"""

from __future__ import annotations

import calendar
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

import requests


TEFAS_START_YEAR_URL = "https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir"
TEFAS_START_YEAR_PERIOD_MONTHS = 60
TEFAS_START_YEAR_TOLERANCE_DAYS = 20
TEFAS_START_YEAR_TIMEOUT_SECONDS = 45
TEFAS_START_YEAR_SOURCE = "TEFAS_FIRST_AVAILABLE_DATE_60M"

TEFAS_START_YEAR_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://www.tefas.gov.tr",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

RETRYABLE_STATUSES = {
    "WAF_REJECTED",
    "REQUEST_ERROR",
    "HTTP_ERROR",
    "JSON_PARSE_ERROR",
    "BLOCKED_SKIPPED",
}


@dataclass(frozen=True)
class TefasStartYearResult:
    fund_code: str
    status: str
    accepted: bool
    start_date: str
    start_year: str
    source: str
    confidence: str
    decision: str
    evidence: str
    http_status: int | None
    content_type: str
    raw_result_count: int
    valid_date_count: int
    positive_price_count: int
    first_available_date: str
    first_available_price: str
    first_positive_date: str
    first_positive_price: str
    last_available_date: str
    five_year_boundary: str
    acceptance_threshold: str
    days_after_boundary: int | None
    error: str

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE_STATUSES


class TefasStartYearRateLimiter:
    """TEFAS başlangıç isteklerini tek sıraya alır ve engelde batch'i susturur."""

    def __init__(self, delay_min_seconds: float = 15.0, delay_max_seconds: float = 20.0) -> None:
        self.delay_min = max(0.0, float(delay_min_seconds))
        self.delay_max = max(self.delay_min, float(delay_max_seconds))
        self._lock = threading.Lock()
        self._last_request_started = 0.0
        self._blocked_reason = ""

    @property
    def blocked_reason(self) -> str:
        with self._lock:
            return self._blocked_reason

    def mark_blocked(self, reason: str) -> None:
        with self._lock:
            self._blocked_reason = str(reason).strip() or "TEFAS erişimi reddedildi."

    def wait_before_request(self) -> tuple[bool, str]:
        """İstek izni verir; daha önce WAF reddi olduysa yeni istek göndermez."""
        with self._lock:
            if self._blocked_reason:
                return False, self._blocked_reason

            now = time.monotonic()
            if self._last_request_started > 0:
                target_interval = random.uniform(self.delay_min, self.delay_max)
                elapsed = now - self._last_request_started
                remaining = target_interval - elapsed
                if remaining > 0:
                    print(
                        f"TEFAS başlangıç koruma beklemesi: {remaining:.1f} saniye.",
                        flush=True,
                    )
                    time.sleep(remaining)
            self._last_request_started = time.monotonic()
            return True, ""


def subtract_months(value: date, months: int) -> date:
    """``dateutil`` bağımlılığı olmadan takvim ayı çıkarır."""
    total_month = value.year * 12 + (value.month - 1) - max(0, int(months))
    year, month_zero = divmod(total_month, 12)
    month = month_zero + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def format_date_tr(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def _property_value(row: Any, names: Iterable[str]) -> Any:
    if not isinstance(row, dict):
        return None
    lowered = {str(key).casefold(): value for key, value in row.items()}
    for name in names:
        if name in row:
            return row[name]
        candidate = lowered.get(name.casefold())
        if candidate is not None:
            return candidate
    return None


def parse_tefas_date(value: Any) -> date | None:
    text = _normalize_text(value)
    if not text:
        return None

    iso_candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_candidate).date()
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def parse_tefas_price(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = _normalize_text(value)
    if not text:
        return None
    candidates = [text, text.replace(".", "").replace(",", "."), text.replace(",", ".")]
    for candidate in candidates:
        try:
            return Decimal(candidate)
        except InvalidOperation:
            continue
    return None


def _price_text(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return format(value, "f")


def _is_waf_rejected(text: str) -> bool:
    folded = text.casefold()
    return any(
        marker in folded
        for marker in (
            "request rejected",
            "requested url was rejected",
            "support id",
        )
    )


def _empty_result(
    fund_code: str,
    *,
    status: str,
    decision: str,
    error: str = "",
    http_status: int | None = None,
    content_type: str = "",
    boundary: date | None = None,
    threshold: date | None = None,
) -> TefasStartYearResult:
    return TefasStartYearResult(
        fund_code=fund_code,
        status=status,
        accepted=False,
        start_date="—",
        start_year="—",
        source=f"TEFAS_START_FALLBACK:{status}",
        confidence="YOK",
        decision=decision,
        evidence=decision,
        http_status=http_status,
        content_type=content_type,
        raw_result_count=0,
        valid_date_count=0,
        positive_price_count=0,
        first_available_date="—",
        first_available_price="—",
        first_positive_date="—",
        first_positive_price="—",
        last_available_date="—",
        five_year_boundary=format_date_tr(boundary) if boundary else "—",
        acceptance_threshold=format_date_tr(threshold) if threshold else "—",
        days_after_boundary=None,
        error=error,
    )


def evaluate_tefas_start_year_payload(
    fund_code: str,
    payload: Any,
    *,
    today: date | None = None,
    period_months: int = TEFAS_START_YEAR_PERIOD_MONTHS,
    tolerance_days: int = TEFAS_START_YEAR_TOLERANCE_DAYS,
    http_status: int | None = 200,
    content_type: str = "application/json",
) -> TefasStartYearResult:
    """TEFAS JSON cevabını fiyat koşulu koymadan başlangıç tarihine dönüştürür."""
    code = _normalize_text(fund_code).upper()
    reference_date = today or date.today()
    boundary = subtract_months(reference_date, period_months)
    threshold = boundary + timedelta(days=max(0, int(tolerance_days)))

    if isinstance(payload, dict):
        rows = payload.get("resultList")
        if rows is None:
            rows = payload.get("ResultList")
    else:
        rows = None
    if not isinstance(rows, list) or not rows:
        return _empty_result(
            code,
            status="EMPTY_RESULT",
            decision="TEFAS resultList boş veya beklenen yapıda değil.",
            http_status=http_status,
            content_type=content_type,
            boundary=boundary,
            threshold=threshold,
        )

    parsed_rows: list[tuple[date, Decimal | None]] = []
    for row in rows:
        parsed_date = parse_tefas_date(
            _property_value(row, ("tarih", "Tarih", "date", "Date"))
        )
        if parsed_date is None:
            continue
        parsed_price = parse_tefas_price(
            _property_value(row, ("fiyat", "Fiyat", "price", "Price"))
        )
        parsed_rows.append((parsed_date, parsed_price))

    parsed_rows.sort(key=lambda item: item[0])
    if not parsed_rows:
        return _empty_result(
            code,
            status="DATE_PARSE_ERROR",
            decision="TEFAS resultList geldi ancak geçerli tarih okunamadı.",
            http_status=http_status,
            content_type=content_type,
            boundary=boundary,
            threshold=threshold,
        )

    positive_rows = [row for row in parsed_rows if row[1] is not None and row[1] > 0]
    first_date, first_price = parsed_rows[0]
    last_date = parsed_rows[-1][0]
    first_positive_date = positive_rows[0][0] if positive_rows else None
    first_positive_price = positive_rows[0][1] if positive_rows else None
    days_after_boundary = (first_date - boundary).days
    accepted = first_date > threshold

    evidence = (
        f"TEFAS 60 ay JSON | kayıt={len(rows)} | geçerli_tarih={len(parsed_rows)} | "
        f"ilk_geçerli={format_date_tr(first_date)} | ilk_fiyat={_price_text(first_price)} | "
        f"ilk_pozitif={format_date_tr(first_positive_date) if first_positive_date else '—'} | "
        f"ilk_pozitif_fiyat={_price_text(first_positive_price)} | "
        f"son_tarih={format_date_tr(last_date)} | doğal_sınır={format_date_tr(boundary)} | "
        f"kabul_eşiği={format_date_tr(threshold)} | sınırdan_gün={days_after_boundary}."
    )

    if accepted:
        if first_price is not None and first_price == 0:
            price_note = " İlk kayıt fiyatı 0 olsa da tarih geçerli kabul edildi."
        else:
            price_note = ""
        if not positive_rows:
            price_note = " Tüm fiyatlar 0/boş olsa da en eski geçerli tarih kullanıldı."
        decision = (
            "En eski geçerli TEFAS tarihi 60 aylık doğal sınırdan "
            f"{max(0, int(tolerance_days))} günden fazla yeni; başlangıç yılı kabul edildi."
            + price_note
        )
        return TefasStartYearResult(
            fund_code=code,
            status="ACCEPTED",
            accepted=True,
            start_date=format_date_tr(first_date),
            start_year=str(first_date.year),
            source=TEFAS_START_YEAR_SOURCE,
            confidence="YÜKSEK",
            decision=decision,
            evidence=evidence + " " + decision,
            http_status=http_status,
            content_type=content_type,
            raw_result_count=len(rows),
            valid_date_count=len(parsed_rows),
            positive_price_count=len(positive_rows),
            first_available_date=format_date_tr(first_date),
            first_available_price=_price_text(first_price),
            first_positive_date=format_date_tr(first_positive_date) if first_positive_date else "—",
            first_positive_price=_price_text(first_positive_price),
            last_available_date=format_date_tr(last_date),
            five_year_boundary=format_date_tr(boundary),
            acceptance_threshold=format_date_tr(threshold),
            days_after_boundary=days_after_boundary,
            error="",
        )

    decision = (
        "En eski geçerli TEFAS tarihi 60 aylık doğal sınıra yakın; seri daha eski bir "
        "fonu kırpıyor olabilir. Başlangıç yılı yazılmadı."
    )
    return TefasStartYearResult(
        fund_code=code,
        status="TRUNCATED",
        accepted=False,
        start_date="—",
        start_year="—",
        source="TEFAS_START_FALLBACK:TRUNCATED",
        confidence="YOK",
        decision=decision,
        evidence=evidence + " " + decision,
        http_status=http_status,
        content_type=content_type,
        raw_result_count=len(rows),
        valid_date_count=len(parsed_rows),
        positive_price_count=len(positive_rows),
        first_available_date=format_date_tr(first_date),
        first_available_price=_price_text(first_price),
        first_positive_date=format_date_tr(first_positive_date) if first_positive_date else "—",
        first_positive_price=_price_text(first_positive_price),
        last_available_date=format_date_tr(last_date),
        five_year_boundary=format_date_tr(boundary),
        acceptance_threshold=format_date_tr(threshold),
        days_after_boundary=days_after_boundary,
        error="",
    )


def fetch_tefas_start_year(
    fund_code: str,
    *,
    session: requests.Session | None = None,
    rate_limiter: TefasStartYearRateLimiter | None = None,
    timeout: int = TEFAS_START_YEAR_TIMEOUT_SECONDS,
    today: date | None = None,
    period_months: int = TEFAS_START_YEAR_PERIOD_MONTHS,
    tolerance_days: int = TEFAS_START_YEAR_TOLERANCE_DAYS,
) -> TefasStartYearResult:
    """Fon başına tek POST isteğiyle TEFAS başlangıç tarihini dener."""
    code = _normalize_text(fund_code).upper()
    reference_date = today or date.today()
    boundary = subtract_months(reference_date, period_months)
    threshold = boundary + timedelta(days=max(0, int(tolerance_days)))

    if rate_limiter is not None:
        allowed, reason = rate_limiter.wait_before_request()
        if not allowed:
            return _empty_result(
                code,
                status="BLOCKED_SKIPPED",
                decision=(
                    "Aynı batch içinde önceki TEFAS isteği WAF tarafından reddedildiği için "
                    "yeni istek gönderilmedi; sonraki workflow çalışmasında tekrar denenecek."
                ),
                error=reason,
                boundary=boundary,
                threshold=threshold,
            )

    client = session or requests.Session()
    headers = dict(TEFAS_START_YEAR_HEADERS)
    headers["Referer"] = f"https://www.tefas.gov.tr/tr/fon-detayli-analiz/{code}"
    body = {"fonKodu": code, "dil": "TR", "periyod": int(period_months)}

    try:
        response = client.post(
            TEFAS_START_YEAR_URL,
            headers=headers,
            json=body,
            timeout=max(1, int(timeout)),
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        return _empty_result(
            code,
            status="REQUEST_ERROR",
            decision="TEFAS başlangıç JSON isteği teknik hata verdi; sonraki çalışmada tekrar denenebilir.",
            error=f"{type(exc).__name__}: {exc}",
            boundary=boundary,
            threshold=threshold,
        )

    content_type = _normalize_text(response.headers.get("Content-Type"))
    body_text = response.text or ""
    if _is_waf_rejected(body_text):
        reason = f"TEFAS WAF reddi (HTTP {response.status_code})."
        if rate_limiter is not None:
            rate_limiter.mark_blocked(reason)
        return _empty_result(
            code,
            status="WAF_REJECTED",
            decision="TEFAS güvenlik katmanı isteği reddetti; mevcut KAP verisi korunur.",
            error=reason,
            http_status=response.status_code,
            content_type=content_type,
            boundary=boundary,
            threshold=threshold,
        )

    if response.status_code != 200:
        return _empty_result(
            code,
            status="HTTP_ERROR",
            decision=f"TEFAS başlangıç JSON isteği HTTP {response.status_code} döndürdü.",
            error=f"HTTP_{response.status_code}",
            http_status=response.status_code,
            content_type=content_type,
            boundary=boundary,
            threshold=threshold,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        return _empty_result(
            code,
            status="JSON_PARSE_ERROR",
            decision="TEFAS cevabı JSON olarak ayrıştırılamadı; sonraki çalışmada tekrar denenebilir.",
            error=f"{type(exc).__name__}: {exc}",
            http_status=response.status_code,
            content_type=content_type,
            boundary=boundary,
            threshold=threshold,
        )

    return evaluate_tefas_start_year_payload(
        code,
        payload,
        today=reference_date,
        period_months=period_months,
        tolerance_days=tolerance_days,
        http_status=response.status_code,
        content_type=content_type,
    )
