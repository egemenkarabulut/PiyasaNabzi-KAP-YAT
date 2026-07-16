#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TEFAS YAT fon evrenini alır, KAP aktif YF listesiyle eşleştirir ve
KAP fon detay sayfalarından üç alanı çıkarır:

- Fonun Halka Arz Tarihi -> start_year
- Risk Değeri -> risk_level
- Alım Satım Yerleri -> transaction_status
    TEFAS geçiyorsa AÇIK
    alan dolu ve TEFAS geçmiyorsa KAPALI
    bilgi yoksa / alan bulunamazsa KONTROL

Çıktı, public GitHub deposundan masaüstü uygulamasının hızlıca indirebileceği
tek JSON dosyasıdır.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests
from bs4 import BeautifulSoup, Tag


PARSER_VERSION = "kap-exact-table-v1.0"
SCHEMA_VERSION = 1

TEFAS_KIND = "YAT"
KAP_LIST_URL = "https://www.kap.org.tr/tr/api/fund/criteria/YF/Y"
KAP_DETAIL_URL = "https://www.kap.org.tr/tr/fon-bilgileri/genel/{permalink}"

DEFAULT_OUTPUT = Path("data/yat_fund_enrichment.json")
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_WORKERS = 8
DEFAULT_RETRIES = 3

_THREAD_LOCAL = threading.local()


@dataclass(frozen=True)
class KapFund:
    code: str
    name: str
    permalink: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def fold_tr(value: Any) -> str:
    text = normalize_text(value).casefold()
    return (
        text.replace("ı", "i")
        .replace("ş", "s")
        .replace("ğ", "g")
        .replace("ç", "c")
        .replace("ö", "o")
        .replace("ü", "u")
    )


def is_missing_text(value: Any) -> bool:
    folded = fold_tr(value)
    return not folded or folded in {
        "-",
        "—",
        "bilgi mevcut degil",
        "mevcut degil",
        "yok",
    }


def browser_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://www.kap.org.tr/tr/YatirimFonlari/YF",
    }


def thread_session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(browser_headers())
        _THREAD_LOCAL.session = session
    return session


def request_with_retry(
    url: str,
    *,
    timeout: int = 45,
    retries: int = DEFAULT_RETRIES,
) -> requests.Response:
    session = thread_session()
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=timeout)
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                raise requests.HTTPError(
                    f"Geçici HTTP durumu: {response.status_code}",
                    response=response,
                )
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep((attempt * 1.5) + random.uniform(0.1, 0.6))

    raise RuntimeError(f"İstek başarısız: {url} — {last_error}")


def fetch_tefas_yat_universe(lookback_days: int) -> dict[str, str]:
    from pytefas import Crawler

    end_date = date.today()
    start_date = end_date - timedelta(days=lookback_days)

    crawler = Crawler(timeout=120, max_retry=6)
    raw = crawler.fetch(
        start_date.isoformat(),
        end_date.isoformat(),
        kind=TEFAS_KIND,
        columns="info",
        fund_code=None,
    )

    if raw is None or raw.empty:
        raise RuntimeError("TEFAS YAT fon evreni boş döndü.")

    required = {"fund_code", "fund_name"}
    missing = required.difference(raw.columns)
    if missing:
        raise RuntimeError(
            "TEFAS yanıtında beklenen sütunlar eksik: "
            + ", ".join(sorted(missing))
        )

    work = raw.copy()
    work["fund_code"] = (
        work["fund_code"].fillna("").astype(str).str.upper().str.strip()
    )
    work["fund_name"] = (
        work["fund_name"].fillna("").astype(str).str.strip()
    )

    if "date" in work.columns:
        work["date"] = pd.to_datetime(work["date"], errors="coerce")
        work = work.sort_values(["fund_code", "date"])

    work = work[work["fund_code"] != ""]
    work = work.drop_duplicates("fund_code", keep="last")

    result = {
        str(row.fund_code).strip().upper(): normalize_text(row.fund_name)
        for row in work.itertuples(index=False)
        if normalize_text(row.fund_code)
    }

    # Yanlış/eksik TEFAS yanıtının mevcut JSON'u ezmesini engeller.
    if len(result) < 1500:
        raise RuntimeError(
            f"TEFAS YAT fon sayısı olağan dışı düşük: {len(result)}. "
            "Çıktı yazılmadı."
        )

    return dict(sorted(result.items()))


def fetch_kap_active_yf() -> dict[str, KapFund]:
    response = request_with_retry(KAP_LIST_URL, timeout=60)
    payload = response.json()

    if isinstance(payload, dict):
        rows = (
            payload.get("data")
            or payload.get("items")
            or payload.get("result")
            or []
        )
    else:
        rows = payload

    if not isinstance(rows, list):
        raise RuntimeError("KAP aktif YF listesi beklenen formatta değil.")

    result: dict[str, KapFund] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = normalize_text(row.get("fundCode")).upper()
        permalink = normalize_text(row.get("fundPermaLink"))
        name = normalize_text(row.get("fundName") or row.get("title"))
        if code and permalink:
            result[code] = KapFund(
                code=code,
                name=name,
                permalink=permalink,
            )

    if len(result) < 1500:
        raise RuntimeError(
            f"KAP aktif YF listesi olağan dışı düşük: {len(result)}."
        )

    return result


def table_rows(table: Tag) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = [
            normalize_text(cell.get_text(" ", strip=True))
            for cell in tr.find_all(["th", "td"])
        ]
        if cells:
            rows.append(cells)
    return rows


def extract_exact_column_values(
    soup: BeautifulSoup,
    header_label: str,
) -> tuple[list[str], str]:
    """
    Header hücresini tam eşleştirir ve sonraki veri satırlarında aynı sütunu okur.
    Başlığın sağındaki hücreye geçmez.
    """
    target = fold_tr(header_label)

    for table in soup.find_all("table"):
        rows = table_rows(table)
        for header_row_index, row in enumerate(rows):
            for column_index, cell in enumerate(row):
                if fold_tr(cell) != target:
                    continue

                values: list[str] = []
                for data_row in rows[header_row_index + 1 :]:
                    if column_index < len(data_row):
                        value = normalize_text(data_row[column_index])
                        if value:
                            values.append(value)

                evidence = " || ".join(
                    " | ".join(item) for item in rows[:8]
                )
                return values, evidence[:4000]

    return [], ""


def parse_start_year(soup: BeautifulSoup) -> tuple[str, str, str]:
    values, evidence = extract_exact_column_values(
        soup,
        "Fonun Halka Arz Tarihi",
    )

    for value in values:
        match = re.search(
            r"\b(?:0?[1-9]|[12]\d|3[01])[./-]"
            r"(?:0?[1-9]|1[0-2])[./-]((?:19|20)\d{2})\b",
            value,
        )
        if match:
            return match.group(1), value, evidence

    return "", "", evidence


def normalize_risk_raw(value: str) -> str:
    value = normalize_text(value)
    value = re.sub(r"\s*/\s*", "/", value)
    value = re.sub(r"\s*,\s*", ", ", value)
    return value


def parse_risk(soup: BeautifulSoup) -> tuple[str, list[int], str]:
    values, evidence = extract_exact_column_values(soup, "Risk Değeri")
    usable = [
        normalize_risk_raw(value)
        for value in values
        if not is_missing_text(value)
    ]

    if not usable:
        return "", [], evidence

    unique: list[str] = []
    for value in usable:
        if value not in unique:
            unique.append(value)

    raw = " | ".join(unique)
    levels = sorted({
        int(match)
        for match in re.findall(r"(?<!\d)([1-7])(?!\d)", raw)
    })
    return raw, levels, evidence


def parse_transaction(
    soup: BeautifulSoup,
) -> tuple[str, str, list[str], str]:
    values, evidence = extract_exact_column_values(
        soup,
        "Alım Satım Yerleri",
    )

    usable: list[str] = []
    for value in values:
        if not is_missing_text(value):
            clean = normalize_text(value)
            if clean not in usable:
                usable.append(clean)

    if not usable:
        return (
            "KONTROL",
            "Alım Satım Yerleri alanı boş veya bilgi mevcut değil.",
            [],
            evidence,
        )

    if any("tefas" in fold_tr(value) for value in usable):
        return (
            "AÇIK",
            "Alım Satım Yerleri alanında TEFAS ifadesi bulundu.",
            usable,
            evidence,
        )

    return (
        "KAPALI",
        "Alım Satım Yerleri alanı dolu ancak TEFAS ifadesi bulunmadı.",
        usable,
        evidence,
    )


def verify_page_code(soup: BeautifulSoup, expected_code: str) -> bool:
    visible = normalize_text(soup.get_text(" ", strip=True)).upper()
    return bool(
        re.search(
            rf"(?<![A-Z0-9]){re.escape(expected_code.upper())}(?![A-Z0-9])",
            visible,
        )
    )


def parse_kap_detail_html(
    html: str,
    expected_code: str,
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    if not verify_page_code(soup, expected_code):
        raise RuntimeError(
            f"KAP fon kodu doğrulanamadı: {expected_code}"
        )

    start_year, start_raw, start_evidence = parse_start_year(soup)
    risk_level, risk_values, risk_evidence = parse_risk(soup)
    (
        transaction_status,
        transaction_reason,
        transaction_places,
        transaction_evidence,
    ) = parse_transaction(soup)

    return {
        "start_year": start_year,
        "start_date_raw": start_raw,
        "risk_level": risk_level,
        "risk_values": risk_values,
        "transaction_status": transaction_status,
        "transaction_reason": transaction_reason,
        "transaction_places": transaction_places,
        "evidence": {
            "start": start_evidence,
            "risk": risk_evidence,
            "transaction": transaction_evidence,
        },
    }


def fetch_one_fund(
    code: str,
    tefas_name: str,
    kap_fund: KapFund,
    retries: int,
) -> dict[str, Any]:
    url = KAP_DETAIL_URL.format(permalink=kap_fund.permalink)
    response = request_with_retry(url, timeout=45, retries=retries)
    parsed = parse_kap_detail_html(response.text, code)

    now = utc_now_iso()
    return {
        "fund_code": code,
        "fund_name": tefas_name,
        "kap_fund_name": kap_fund.name,
        "start_year": parsed["start_year"],
        "start_date_raw": parsed["start_date_raw"],
        "risk_level": parsed["risk_level"],
        "risk_values": parsed["risk_values"],
        "transaction_status": parsed["transaction_status"],
        "transaction_reason": parsed["transaction_reason"],
        "transaction_places": parsed["transaction_places"],
        "source": "KAP fon detay sayfası",
        "source_url": url,
        "kap_permalink": kap_fund.permalink,
        "last_checked_at": now,
        "last_attempt_at": now,
        "last_attempt_status": "OK",
        "last_error": "",
    }


def load_previous_records(output_path: Path) -> dict[str, dict[str, Any]]:
    if not output_path.exists():
        return {}

    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        funds = payload.get("funds", {})
        return funds if isinstance(funds, dict) else {}
    except Exception:
        return {}


def build_error_record(
    *,
    code: str,
    tefas_name: str,
    kap_fund: KapFund | None,
    previous: dict[str, Any] | None,
    error: str,
) -> dict[str, Any]:
    now = utc_now_iso()
    record = dict(previous or {})

    record.update({
        "fund_code": code,
        "fund_name": tefas_name,
        "kap_fund_name": kap_fund.name if kap_fund else "",
        "kap_permalink": kap_fund.permalink if kap_fund else "",
        "source_url": (
            KAP_DETAIL_URL.format(permalink=kap_fund.permalink)
            if kap_fund
            else ""
        ),
        "last_attempt_at": now,
        "last_attempt_status": "ERROR",
        "last_error": normalize_text(error)[:1000],
    })

    record.setdefault("start_year", "")
    record.setdefault("start_date_raw", "")
    record.setdefault("risk_level", "")
    record.setdefault("risk_values", [])
    record.setdefault("transaction_reason", "")
    record.setdefault("transaction_places", [])
    record.setdefault("source", "KAP fon detay sayfası")
    record.setdefault("last_checked_at", "")

    # Daha önce doğrulanmış değer yoksa kullanıcıya yanlış KAPALI/AÇIK vermeyiz.
    if record.get("transaction_status") not in {"AÇIK", "KAPALI"}:
        record["transaction_status"] = "KONTROL"
        record["transaction_reason"] = (
            "KAP detay sayfası doğrulanamadı; manuel kontrol gerekli."
        )

    return record


def write_json_atomic(output_path: Path, payload: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(output_path)


def summarize_status(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    summary = {"AÇIK": 0, "KAPALI": 0, "KONTROL": 0}
    for record in records:
        status = str(record.get("transaction_status") or "KONTROL")
        if status not in summary:
            status = "KONTROL"
        summary[status] += 1
    return summary


def run_update(
    *,
    output_path: Path,
    workers: int,
    retries: int,
    lookback_days: int,
) -> dict[str, Any]:
    started = time.monotonic()
    generated_at = utc_now_iso()

    print("TEFAS YAT fon evreni alınıyor...")
    tefas_funds = fetch_tefas_yat_universe(lookback_days)
    print(f"TEFAS YAT benzersiz fon: {len(tefas_funds)}")

    print("KAP aktif YF listesi alınıyor...")
    kap_funds = fetch_kap_active_yf()
    print(f"KAP aktif YF fon: {len(kap_funds)}")

    previous = load_previous_records(output_path)
    records: dict[str, dict[str, Any]] = {}

    matched_codes = [
        code for code in tefas_funds
        if code in kap_funds
    ]
    missing_kap_codes = [
        code for code in tefas_funds
        if code not in kap_funds
    ]

    print(
        f"KAP ile eşleşen: {len(matched_codes)} | "
        f"KAP detay bağlantısı bulunamayan: {len(missing_kap_codes)}"
    )

    for code in missing_kap_codes:
        records[code] = build_error_record(
            code=code,
            tefas_name=tefas_funds[code],
            kap_fund=None,
            previous=previous.get(code),
            error="KAP aktif YF listesinde fon kodu bulunamadı.",
        )

    success_count = 0
    error_count = len(missing_kap_codes)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_map = {
            executor.submit(
                fetch_one_fund,
                code,
                tefas_funds[code],
                kap_funds[code],
                retries,
            ): code
            for code in matched_codes
        }

        total = len(future_map)
        for completed, future in enumerate(as_completed(future_map), start=1):
            code = future_map[future]
            try:
                records[code] = future.result()
                success_count += 1
            except Exception as exc:
                records[code] = build_error_record(
                    code=code,
                    tefas_name=tefas_funds[code],
                    kap_fund=kap_funds.get(code),
                    previous=previous.get(code),
                    error=f"{type(exc).__name__}: {exc}",
                )
                error_count += 1

            if completed % 50 == 0 or completed == total:
                print(
                    f"İlerleme: {completed}/{total} | "
                    f"Başarılı: {success_count} | Hata/Kontrol: {error_count}"
                )

    ordered_records = {
        code: records[code]
        for code in sorted(tefas_funds)
    }

    duration = round(time.monotonic() - started, 2)
    status_counts = summarize_status(ordered_records.values())

    payload = {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "generated_at": generated_at,
        "tefas_kind": TEFAS_KIND,
        "tefas_lookback_days": lookback_days,
        "fund_count": len(ordered_records),
        "tefas_fund_count": len(tefas_funds),
        "kap_active_yf_count": len(kap_funds),
        "kap_matched_count": len(matched_codes),
        "successful_detail_count": success_count,
        "error_or_unmatched_count": error_count,
        "transaction_status_counts": status_counts,
        "duration_seconds": duration,
        "sources": {
            "tefas": "TEFAS YAT günlük veri evreni",
            "kap_list": KAP_LIST_URL,
            "kap_detail": KAP_DETAIL_URL,
        },
        "rules": {
            "start_year": "Fonun Halka Arz Tarihi sütununun aynı kolonundaki tarih",
            "risk_level": "Risk Değeri sütununun aynı kolonundaki değer",
            "transaction_status": {
                "AÇIK": "Alım Satım Yerleri alanında TEFAS geçiyor",
                "KAPALI": "Alım Satım Yerleri dolu, TEFAS geçmiyor",
                "KONTROL": "Alan/bilgi yok veya sayfa doğrulanamadı",
            },
        },
        "funds": ordered_records,
    }

    write_json_atomic(output_path, payload)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.getenv("KAP_WORKERS", DEFAULT_WORKERS)),
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=int(os.getenv("KAP_RETRIES", DEFAULT_RETRIES)),
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(
            os.getenv("TEFAS_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS)
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = run_update(
        output_path=args.output,
        workers=args.workers,
        retries=args.retries,
        lookback_days=args.lookback_days,
    )

    print("\nTamamlandı")
    print(f"Çıktı       : {args.output}")
    print(f"Fon sayısı  : {payload['fund_count']}")
    print(
        "İşlem durumu: "
        + json.dumps(
            payload["transaction_status_counts"],
            ensure_ascii=False,
        )
    )
    print(f"Süre        : {payload['duration_seconds']} saniye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
