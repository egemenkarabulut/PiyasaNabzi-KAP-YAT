#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TEFAS profil/risk/işlem durumu zenginleştirme kaynağı.

Doğrulanmış üretim kuralları:
- Tekil profil: ``fonProfilBilgiGetir``
  - ``riskDegeri``
  - ``tefasDurum``
- Toplu risk: ``fonGetiriBazliBilgiGetir``
  - ``riskDegeri``
  - ``tefasDurum`` yalnız teşhis/fallback kanıtıdır.
- Risk yalnız doğrulanmış alan adı ve 1–7 değeri ile kabul edilir.
- ``null``, boş ve ``-`` risk değildir; değer uydurulmaz.
- İşlem durumunda tekil profil ``tefasDurum`` birincil TEFAS kaynağıdır.
- ``getFplFonList`` sonucu ayrı doğrulama olarak publisher tarafından verilir.
- Türkçe büyük ``İ`` Unicode normalizasyonu güvenli biçimde ele alınır.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

TEFAS_PROFILE_URL = "https://www.tefas.gov.tr/api/funds/fonProfilBilgiGetir"
TEFAS_BULK_RISK_URL = "https://www.tefas.gov.tr/api/funds/fonGetiriBazliBilgiGetir"

TEFAS_PROFILE_TIMEOUT_SECONDS = 45
TEFAS_BULK_TIMEOUT_SECONDS = 75

TEFAS_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://www.tefas.gov.tr",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

PROFILE_RETRYABLE_STATUSES = {
    "WAF_REJECTED",
    "REQUEST_ERROR",
    "HTTP_ERROR",
    "JSON_PARSE_ERROR",
    "BLOCKED_SKIPPED",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def fold_tr(value: Any) -> str:
    """Türkçe ve birleşik Unicode işaretlerini karşılaştırma için sadeleştirir."""
    text = normalize_text(value).casefold()
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )
    text = text.translate(
        str.maketrans(
            {
                "ı": "i",
                "ş": "s",
                "ğ": "g",
                "ç": "c",
                "ö": "o",
                "ü": "u",
            }
        )
    )
    return re.sub(r"\s+", " ", text).strip()


def property_value(row: Any, names: Iterable[str]) -> Any:
    if not isinstance(row, dict):
        return None
    lowered = {str(key).casefold(): value for key, value in row.items()}
    for name in names:
        if name in row:
            return row[name]
        folded = lowered.get(name.casefold())
        if folded is not None:
            return folded
    return None


def parse_risk_value(value: Any) -> int | None:
    """Yalnız 1–7 veya ``n/7`` biçimindeki doğrulanmış risk değerini kabul eder."""
    text = normalize_text(value)
    match = re.fullmatch(r"([1-7])(?:\s*/\s*7)?", text)
    return int(match.group(1)) if match else None


def normalize_tefas_status(value: Any) -> str:
    """TEFAS ham durumunu ``AÇIK`` / ``KAPALI`` / ``KONTROL`` yapar."""
    if value is None:
        return "KONTROL"
    if isinstance(value, bool):
        return "AÇIK" if value else "KAPALI"
    if isinstance(value, int) and not isinstance(value, bool):
        if value == 1:
            return "AÇIK"
        if value == 0:
            return "KAPALI"

    text = fold_tr(value)
    if not text:
        return "KONTROL"

    open_exact = {
        "1",
        "true",
        "evet",
        "aktif",
        "acik",
        "tefas",
        "tefas dahil",
        "tefas'ta islem goruyor",
        "tefasta islem goruyor",
    }
    closed_exact = {
        "0",
        "false",
        "hayir",
        "pasif",
        "kapali",
        "tefas disi",
        "tefas harici",
        "tefas'ta islem gormuyor",
        "tefasta islem gormuyor",
    }
    if text in open_exact:
        return "AÇIK"
    if text in closed_exact:
        return "KAPALI"

    if re.search(r"\bislem goruyor\b", text):
        return "AÇIK"
    if re.search(r"\bislem gormuyor\b", text):
        return "KAPALI"
    if re.search(r"\balima acik\b|\bsatisa acik\b|tefas.*aktif", text):
        return "AÇIK"
    if re.search(r"\balima kapali\b|\bsatisa kapali\b|tefas.*pasif", text):
        return "KAPALI"
    return "KONTROL"


def is_waf_rejected(text: str) -> bool:
    folded = fold_tr(text)
    return any(
        marker in folded
        for marker in (
            "request rejected",
            "requested url was rejected",
            "support id",
        )
    )


def write_raw(path: Path | None, text: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TefasApiRateLimiter:
    """Profil ve başlangıç POST isteklerini tek güvenli sıraya alır."""

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
            self._blocked_reason = normalize_text(reason) or "TEFAS erişimi reddedildi."

    def wait_before_request(self) -> tuple[bool, str]:
        with self._lock:
            if self._blocked_reason:
                return False, self._blocked_reason
            now = time.monotonic()
            if self._last_request_started > 0:
                interval = random.uniform(self.delay_min, self.delay_max)
                remaining = interval - (now - self._last_request_started)
                if remaining > 0:
                    print(f"TEFAS API koruma beklemesi: {remaining:.1f} saniye.", flush=True)
                    time.sleep(remaining)
            self._last_request_started = time.monotonic()
            return True, ""


@dataclass(frozen=True)
class TefasProfileResult:
    fund_code: str
    status: str
    found: bool
    checked_at: str
    http_status: int | None
    content_type: str
    fund_name: str
    isin_code: str
    kap_link: str
    risk_raw: str
    risk_value: int | None
    status_raw: str
    status_normalized: str
    error: str

    @property
    def retryable(self) -> bool:
        return self.status in PROFILE_RETRYABLE_STATUSES


@dataclass(frozen=True)
class TefasBulkFundRow:
    fund_code: str
    fund_name: str
    fund_type: str
    risk_raw: str
    risk_value: int | None
    status_raw: str
    status_normalized: str


@dataclass(frozen=True)
class TefasBulkSnapshot:
    status: str
    checked_at: str
    http_status: int | None
    content_type: str
    row_count: int
    rows: dict[str, TefasBulkFundRow]
    error: str


@dataclass(frozen=True)
class RiskDecision:
    final_value: str
    final_source: str
    final_evidence: str
    final_confidence: str
    profile_raw: str
    profile_value: str
    bulk_raw: str
    bulk_value: str
    tefas_comparison: str
    conflict_flag: str


@dataclass(frozen=True)
class TradeDecision:
    final_status: str
    final_source: str
    final_evidence: str
    final_confidence: str
    final_reason: str
    kap_comparison: str
    conflict_flag: str
    tefas_internal_conflict: str


def empty_profile_result(
    fund_code: str,
    *,
    status: str,
    error: str = "",
    http_status: int | None = None,
    content_type: str = "",
) -> TefasProfileResult:
    return TefasProfileResult(
        fund_code=normalize_text(fund_code).upper(),
        status=status,
        found=False,
        checked_at=utc_now_iso(),
        http_status=http_status,
        content_type=content_type,
        fund_name="",
        isin_code="",
        kap_link="",
        risk_raw="",
        risk_value=None,
        status_raw="",
        status_normalized="KONTROL",
        error=normalize_text(error),
    )


def evaluate_profile_payload(
    fund_code: str,
    payload: Any,
    *,
    http_status: int | None = 200,
    content_type: str = "application/json",
) -> TefasProfileResult:
    code = normalize_text(fund_code).upper()
    rows = payload.get("resultList") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return empty_profile_result(
            code,
            status="PROFILE_EMPTY",
            http_status=http_status,
            content_type=content_type,
            error="TEFAS profil resultList boş veya beklenen yapıda değil.",
        )

    row = next(
        (
            item
            for item in rows
            if normalize_text(property_value(item, ("fonKodu", "FonKodu", "fundCode"))).upper()
            == code
        ),
        rows[0],
    )
    risk_raw_value = property_value(row, ("riskDegeri", "RiskDegeri", "riskValue"))
    status_raw_value = property_value(row, ("tefasDurum", "TefasDurum", "tefasStatus"))
    return TefasProfileResult(
        fund_code=code,
        status="API_OK",
        found=True,
        checked_at=utc_now_iso(),
        http_status=http_status,
        content_type=normalize_text(content_type),
        fund_name=normalize_text(property_value(row, ("fonUnvan", "FonUnvan", "fundName"))),
        isin_code=normalize_text(property_value(row, ("isinKodu", "IsinKodu", "isin"))),
        kap_link=normalize_text(property_value(row, ("kapLink", "KapLink"))),
        risk_raw=normalize_text(risk_raw_value),
        risk_value=parse_risk_value(risk_raw_value),
        status_raw=normalize_text(status_raw_value),
        status_normalized=normalize_tefas_status(status_raw_value),
        error="",
    )


def fetch_tefas_profile(
    fund_code: str,
    *,
    session: requests.Session | None = None,
    rate_limiter: TefasApiRateLimiter | None = None,
    raw_path: Path | None = None,
    timeout: int = TEFAS_PROFILE_TIMEOUT_SECONDS,
) -> TefasProfileResult:
    code = normalize_text(fund_code).upper()
    if rate_limiter is not None:
        allowed, reason = rate_limiter.wait_before_request()
        if not allowed:
            return empty_profile_result(code, status="BLOCKED_SKIPPED", error=reason)

    client = session or requests.Session()
    headers = dict(TEFAS_HEADERS)
    headers["Referer"] = f"https://www.tefas.gov.tr/tr/fon-detayli-analiz/{code}"
    try:
        response = client.post(
            TEFAS_PROFILE_URL,
            json={"fonKodu": code, "dil": "TR"},
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
        )
        body = response.text
        write_raw(raw_path, body)
        content_type = normalize_text(response.headers.get("Content-Type"))
        if is_waf_rejected(body):
            if rate_limiter is not None:
                rate_limiter.mark_blocked("TEFAS profil isteği WAF tarafından reddedildi.")
            return empty_profile_result(
                code,
                status="WAF_REJECTED",
                http_status=response.status_code,
                content_type=content_type,
            )
        if response.status_code != 200:
            return empty_profile_result(
                code,
                status="HTTP_ERROR",
                http_status=response.status_code,
                content_type=content_type,
                error=f"HTTP {response.status_code}",
            )
        try:
            payload = response.json()
        except Exception as exc:
            return empty_profile_result(
                code,
                status="JSON_PARSE_ERROR",
                http_status=response.status_code,
                content_type=content_type,
                error=f"{type(exc).__name__}: {exc}",
            )
        return evaluate_profile_payload(
            code,
            payload,
            http_status=response.status_code,
            content_type=content_type,
        )
    except Exception as exc:
        return empty_profile_result(
            code,
            status="REQUEST_ERROR",
            error=f"{type(exc).__name__}: {exc}",
        )


def evaluate_bulk_payload(
    payload: Any,
    *,
    http_status: int | None = 200,
    content_type: str = "application/json",
) -> TefasBulkSnapshot:
    source_rows = payload.get("resultList") if isinstance(payload, dict) else None
    if not isinstance(source_rows, list):
        return TefasBulkSnapshot(
            status="BULK_EMPTY",
            checked_at=utc_now_iso(),
            http_status=http_status,
            content_type=normalize_text(content_type),
            row_count=0,
            rows={},
            error="TEFAS toplu resultList boş veya beklenen yapıda değil.",
        )

    rows: dict[str, TefasBulkFundRow] = {}
    for row in source_rows:
        code = normalize_text(property_value(row, ("fonKodu", "FonKodu", "fundCode"))).upper()
        if not code:
            continue
        risk_raw_value = property_value(row, ("riskDegeri", "RiskDegeri", "riskValue"))
        status_raw_value = property_value(row, ("tefasDurum", "TefasDurum", "tefasStatus"))
        rows[code] = TefasBulkFundRow(
            fund_code=code,
            fund_name=normalize_text(property_value(row, ("fonUnvan", "FonUnvan", "fundName"))),
            fund_type=normalize_text(
                property_value(row, ("fonTurAciklama", "FonTurAciklama", "fundType"))
            ),
            risk_raw=normalize_text(risk_raw_value),
            risk_value=parse_risk_value(risk_raw_value),
            status_raw=normalize_text(status_raw_value),
            status_normalized=normalize_tefas_status(status_raw_value),
        )
    return TefasBulkSnapshot(
        status="API_OK",
        checked_at=utc_now_iso(),
        http_status=http_status,
        content_type=normalize_text(content_type),
        row_count=len(rows),
        rows=rows,
        error="",
    )


def fetch_tefas_bulk_snapshot(
    *,
    session: requests.Session | None = None,
    raw_path: Path | None = None,
    timeout: int = TEFAS_BULK_TIMEOUT_SECONDS,
) -> TefasBulkSnapshot:
    payload = {
        "fonTipi": "YAT",
        "dil": "TR",
        "calismaTipi": 2,
        "donemGetiri1a": "1",
        "donemGetiri3a": "1",
        "donemGetiri6a": "1",
        "donemGetiriyb": "1",
        "donemGetiri1y": "1",
        "donemGetiri3y": "1",
        "donemGetiri5y": "1",
    }
    client = session or requests.Session()
    headers = dict(TEFAS_HEADERS)
    headers["Referer"] = "https://www.tefas.gov.tr/tr/fon-karsilastirma"
    try:
        response = client.post(
            TEFAS_BULK_RISK_URL,
            json=payload,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
        )
        body = response.text
        write_raw(raw_path, body)
        content_type = normalize_text(response.headers.get("Content-Type"))
        if is_waf_rejected(body):
            return TefasBulkSnapshot(
                status="WAF_REJECTED",
                checked_at=utc_now_iso(),
                http_status=response.status_code,
                content_type=content_type,
                row_count=0,
                rows={},
                error="TEFAS toplu risk isteği WAF tarafından reddedildi.",
            )
        if response.status_code != 200:
            return TefasBulkSnapshot(
                status="HTTP_ERROR",
                checked_at=utc_now_iso(),
                http_status=response.status_code,
                content_type=content_type,
                row_count=0,
                rows={},
                error=f"HTTP {response.status_code}",
            )
        try:
            parsed = response.json()
        except Exception as exc:
            return TefasBulkSnapshot(
                status="JSON_PARSE_ERROR",
                checked_at=utc_now_iso(),
                http_status=response.status_code,
                content_type=content_type,
                row_count=0,
                rows={},
                error=f"{type(exc).__name__}: {exc}",
            )
        return evaluate_bulk_payload(
            parsed,
            http_status=response.status_code,
            content_type=content_type,
        )
    except Exception as exc:
        return TefasBulkSnapshot(
            status="REQUEST_ERROR",
            checked_at=utc_now_iso(),
            http_status=None,
            content_type="",
            row_count=0,
            rows={},
            error=f"{type(exc).__name__}: {exc}",
        )


def resolve_risk(
    *,
    kap_risk: Any,
    kap_source: str,
    kap_evidence: str,
    kap_confidence: str,
    profile_risk_raw: Any,
    bulk_risk_raw: Any,
) -> RiskDecision:
    kap_value = parse_risk_value(kap_risk)
    profile_value = parse_risk_value(profile_risk_raw)
    bulk_value = parse_risk_value(bulk_risk_raw)

    if profile_value is not None and bulk_value is not None:
        tefas_comparison = "EŞLEŞİYOR" if profile_value == bulk_value else "ÇATIŞMA"
    elif profile_value is not None or bulk_value is not None:
        tefas_comparison = "TEK_KAYNAK"
    else:
        tefas_comparison = "BOŞ"

    tefas_values = {value for value in (profile_value, bulk_value) if value is not None}
    conflict = bool(len(tefas_values) > 1)
    if kap_value is not None and any(value != kap_value for value in tefas_values):
        conflict = True

    profile_text = normalize_text(profile_risk_raw)
    bulk_text = normalize_text(bulk_risk_raw)

    if kap_value is not None:
        evidence = normalize_text(kap_evidence)
        if tefas_values:
            evidence = normalize_text(
                f"{evidence} | TEFAS profil={profile_text or 'null'}; "
                f"TEFAS toplu={bulk_text or 'null'}; karşılaştırma={tefas_comparison}"
            )
        return RiskDecision(
            final_value=str(kap_value),
            final_source=normalize_text(kap_source) or "KAP",
            final_evidence=evidence,
            final_confidence=normalize_text(kap_confidence) or "YÜKSEK",
            profile_raw=profile_text,
            profile_value="" if profile_value is None else str(profile_value),
            bulk_raw=bulk_text,
            bulk_value="" if bulk_value is None else str(bulk_value),
            tefas_comparison=tefas_comparison,
            conflict_flag="EVET" if conflict else "HAYIR",
        )

    if profile_value is not None and bulk_value is not None and profile_value == bulk_value:
        return RiskDecision(
            final_value=str(profile_value),
            final_source="TEFAS_PROFILE+riskDegeri + TEFAS_BULK+riskDegeri",
            final_evidence=f"TEFAS profil riskDegeri={profile_text}; TEFAS toplu riskDegeri={bulk_text}.",
            final_confidence="ÇOK YÜKSEK",
            profile_raw=profile_text,
            profile_value=str(profile_value),
            bulk_raw=bulk_text,
            bulk_value=str(bulk_value),
            tefas_comparison="EŞLEŞİYOR",
            conflict_flag="HAYIR",
        )
    if profile_value is not None and bulk_value is None:
        return RiskDecision(
            final_value=str(profile_value),
            final_source="TEFAS_PROFILE:riskDegeri",
            final_evidence=f"TEFAS profil riskDegeri={profile_text}; toplu risk boş.",
            final_confidence="YÜKSEK",
            profile_raw=profile_text,
            profile_value=str(profile_value),
            bulk_raw=bulk_text,
            bulk_value="",
            tefas_comparison="TEK_KAYNAK",
            conflict_flag="HAYIR",
        )
    if bulk_value is not None and profile_value is None:
        return RiskDecision(
            final_value=str(bulk_value),
            final_source="TEFAS_BULK_RETURNS:riskDegeri",
            final_evidence=f"TEFAS toplu riskDegeri={bulk_text}; profil risk boş.",
            final_confidence="YÜKSEK",
            profile_raw=profile_text,
            profile_value="",
            bulk_raw=bulk_text,
            bulk_value=str(bulk_value),
            tefas_comparison="TEK_KAYNAK",
            conflict_flag="HAYIR",
        )
    if profile_value is not None and bulk_value is not None and profile_value != bulk_value:
        return RiskDecision(
            final_value="—",
            final_source="TEFAS_RISK_CONFLICT",
            final_evidence=f"TEFAS profil={profile_text}; TEFAS toplu={bulk_text}; otomatik risk yazılmadı.",
            final_confidence="YOK",
            profile_raw=profile_text,
            profile_value=str(profile_value),
            bulk_raw=bulk_text,
            bulk_value=str(bulk_value),
            tefas_comparison="ÇATIŞMA",
            conflict_flag="EVET",
        )
    return RiskDecision(
        final_value="—",
        final_source=normalize_text(kap_source) or "SOURCE_NOT_FOUND",
        final_evidence=normalize_text(kap_evidence) or "KAP ve TEFAS kaynaklarında risk değeri yayımlanmadı.",
        final_confidence="YOK",
        profile_raw=profile_text,
        profile_value="",
        bulk_raw=bulk_text,
        bulk_value="",
        tefas_comparison="BOŞ",
        conflict_flag="HAYIR",
    )


def resolve_trade_status(
    *,
    kap_status: str,
    kap_source: str,
    kap_evidence: str,
    profile_status_raw: Any,
    profile_status_normalized: str,
    traded_list_match: str,
    traded_list_status_raw: Any,
    traded_list_error: str = "",
) -> TradeDecision:
    kap = normalize_text(kap_status).upper()
    profile = normalize_text(profile_status_normalized).upper()
    list_match = normalize_text(traded_list_match).upper()
    list_status = normalize_tefas_status(traded_list_status_raw)
    profile_raw = normalize_text(profile_status_raw)

    final = kap if kap in {"AÇIK", "KAPALI"} else "BİLİNMİYOR"
    source = normalize_text(kap_source) or "KAP_DETAIL_HTML"
    confidence = "YÜKSEK" if final in {"AÇIK", "KAPALI"} else "YOK"
    internal_conflict = "HAYIR"

    if profile in {"AÇIK", "KAPALI"}:
        final = profile
        if profile == "AÇIK" and list_match == "EVET":
            source = "TEFAS_PROFILE:tefasDurum + TEFAS:getFplFonList"
            confidence = "ÇOK YÜKSEK"
        elif profile == "KAPALI" and list_match == "HAYIR" and not traded_list_error:
            source = "TEFAS_PROFILE:tefasDurum + TEFAS_LIST_ABSENCE"
            confidence = "ÇOK YÜKSEK"
        else:
            source = "TEFAS_PROFILE:tefasDurum"
            confidence = "YÜKSEK"
            if not traded_list_error and list_match in {"EVET", "HAYIR"}:
                expected_match = "EVET" if profile == "AÇIK" else "HAYIR"
                if list_match != expected_match:
                    internal_conflict = "EVET"
    elif list_match == "EVET" or list_status == "AÇIK":
        final = "AÇIK"
        source = "TEFAS:getFplFonList"
        confidence = "YÜKSEK"

    kap_comparison = "KONTROL"
    if kap in {"AÇIK", "KAPALI"} and final in {"AÇIK", "KAPALI"}:
        kap_comparison = "EŞLEŞİYOR" if kap == final else "ÇATIŞMA"

    conflict = internal_conflict == "EVET" or kap_comparison == "ÇATIŞMA"
    evidence = normalize_text(
        f"TEFAS profil tefasDurum={profile_raw or 'boş'} => {profile or 'KONTROL'}; "
        f"getFplFonList eşleşmesi={list_match or 'HATA'}; "
        f"liste durum={normalize_text(traded_list_status_raw) or 'boş'}; "
        f"KAP sonucu={kap or 'BİLİNMİYOR'}; KAP kanıtı={normalize_text(kap_evidence)}"
    )
    reason = (
        "TEFAS profilindeki açık tefasDurum nihai işlem durumu olarak kabul edildi; "
        "TEFAS işlem listesi doğrulama kaynağıdır."
        if profile in {"AÇIK", "KAPALI"}
        else "Profil durumu çözülemedi; mevcut KAP/TEFAS işlem listesi fallback sonucu kullanıldı."
    )
    return TradeDecision(
        final_status=final,
        final_source=source,
        final_evidence=evidence,
        final_confidence=confidence,
        final_reason=reason,
        kap_comparison=kap_comparison,
        conflict_flag="EVET" if conflict else "HAYIR",
        tefas_internal_conflict=internal_conflict,
    )
