#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Piyasa Nabzı Türkiye — KAP YF/Y resumable public-data publisher.

The extractor rules are imported from ``kap_yat_source.py`` (v9.1 smart-wait).
This file adds GitHub-safe batching, persistent checkpoints, diagnostics and
final publication without replacing previously verified data with temporary
HTTP failures.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kap_yat_source import (
    DEFAULT_DELAY_SECONDS,
    FundEntry,
    FundResult,
    GlobalRateLimiter,
    SCRIPT_VERSION as SOURCE_ENGINE_VERSION,
    failure_category,
    fetch_kap_fund_list,
    fetch_tefas_traded_funds,
    merge_results,
    normalize_text,
    run_internal_rule_self_test,
    test_one_fund,
)

PUBLISHER_VERSION = "github-resumable-v2.0"
SCHEMA_VERSION = 2

DATA_DIR = Path("data")
OFFICIAL_PATH = DATA_DIR / "yat_fund_enrichment.json"
PROGRESS_PATH = DATA_DIR / "staging" / "yat_kap_progress.json"
FAILED_CODES_PATH = DATA_DIR / "staging" / "failed_codes.json"
REQUEST_FAILURES_PATH = DATA_DIR / "diagnostics" / "request_failures.json"
RUN_STATE_PATH = DATA_DIR / "run_state.json"
ATTEMPT_EVENTS_PATH = DATA_DIR / "diagnostics" / "attempt_events.jsonl"

DEFAULT_BATCH_SIZE = 60
DEFAULT_REFRESH_DAYS = 6
DEFAULT_MAX_FIELD_ATTEMPTS = 3
DEFAULT_MAX_TECHNICAL_ATTEMPTS = 6
MIN_EXPECTED_FUNDS = 2000
MIN_VALID_PAGE_RATIO = 0.98
MIN_KNOWN_TRADE_RATIO = 0.98


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_progress() -> tuple[dict[str, FundResult], dict[str, int]]:
    payload = load_json(PROGRESS_PATH, {})
    rows = payload.get("results", {}) if isinstance(payload, dict) else {}
    attempts_raw = payload.get("attempt_counts", {}) if isinstance(payload, dict) else {}
    allowed = set(FundResult.__annotations__)
    results: dict[str, FundResult] = {}
    if isinstance(rows, dict):
        for code, row in rows.items():
            if not isinstance(row, dict):
                continue
            if not allowed.issubset(row):
                continue
            try:
                results[str(code).upper()] = FundResult(
                    **{key: row[key] for key in allowed}
                )
            except Exception:
                continue
    attempts = {
        str(code).upper(): max(0, int(value))
        for code, value in (attempts_raw.items() if isinstance(attempts_raw, dict) else [])
    }
    return results, attempts


def is_missing(value: Any) -> bool:
    return normalize_text(value) in {"", "—", "-"}


def is_valid_page(result: FundResult | None) -> bool:
    return bool(
        result
        and not result.error
        and result.http_status == 200
        and result.page_code_verified == "EVET"
    )


def is_known_trade(result: FundResult | None) -> bool:
    return bool(result and result.transaction_status in {"AÇIK", "KAPALI"})


def needs_technical_retry(result: FundResult | None) -> bool:
    if result is None:
        return True
    return not is_valid_page(result) or not is_known_trade(result)


def needs_field_retry(result: FundResult | None) -> bool:
    return bool(
        result
        and is_valid_page(result)
        and is_known_trade(result)
        and (is_missing(result.start_year) or is_missing(result.risk_level))
    )


def parse_result_time(result: FundResult | None) -> datetime | None:
    if not result:
        return None
    text = normalize_text(result.test_time)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def is_stale(result: FundResult | None, refresh_days: int) -> bool:
    checked = parse_result_time(result)
    if checked is None:
        return True
    return checked <= utc_now() - timedelta(days=max(0, refresh_days))


def choose_batch(
    all_codes: list[str],
    progress: dict[str, FundResult],
    attempts: dict[str, int],
    *,
    batch_size: int,
    refresh_days: int,
    max_field_attempts: int,
    max_technical_attempts: int,
) -> tuple[list[str], dict[str, int]]:
    unattempted: list[str] = []
    technical: list[str] = []
    incomplete: list[str] = []
    stale: list[str] = []

    for code in all_codes:
        result = progress.get(code)
        count = attempts.get(code, 0)
        if result is None:
            unattempted.append(code)
        elif needs_technical_retry(result) and count < max_technical_attempts:
            technical.append(code)
        elif needs_field_retry(result) and count < max_field_attempts:
            incomplete.append(code)
        elif is_stale(result, refresh_days):
            stale.append(code)

    ordered = unattempted + technical + incomplete + stale
    selected = ordered[: max(1, batch_size)]
    return selected, {
        "unattempted": len(unattempted),
        "technical_retryable": len(technical),
        "field_retryable": len(incomplete),
        "stale": len(stale),
        "pending_total": len(ordered),
    }


def append_attempt_event(result: FundResult, attempt_count: int) -> None:
    ATTEMPT_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "time": utc_now_iso(),
        "fund_code": result.fund_code,
        "attempt_count": attempt_count,
        "category": failure_category(result),
        "http_status": result.http_status,
        "page_code_verified": result.page_code_verified,
        "start_year": result.start_year,
        "risk_level": result.risk_level,
        "trade_status": result.transaction_status,
        "error": result.error,
    }
    with ATTEMPT_EVENTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def save_progress(
    progress: dict[str, FundResult],
    attempts: dict[str, int],
    *,
    total_funds: int,
) -> None:
    atomic_write_json(PROGRESS_PATH, {
        "schema_version": SCHEMA_VERSION,
        "publisher_version": PUBLISHER_VERSION,
        "source_engine_version": SOURCE_ENGINE_VERSION,
        "updated_at": utc_now_iso(),
        "total_kap_yf_count": total_funds,
        "saved_result_count": len(progress),
        "attempt_counts": dict(sorted(attempts.items())),
        "results": {
            code: asdict(result)
            for code, result in sorted(progress.items())
        },
    })


def public_record(result: FundResult, attempts: int) -> dict[str, Any]:
    start_missing = is_missing(result.start_year)
    risk_missing = is_missing(result.risk_level)
    trade_known = is_known_trade(result)
    page_valid = is_valid_page(result)
    trade_status = result.transaction_status if trade_known else "KONTROL"
    return {
        "fund_code": result.fund_code,
        "fund_name": result.fund_name,
        "start_year": "" if start_missing else result.start_year,
        "risk_level": "" if risk_missing else result.risk_level,
        "risk_detail": "" if is_missing(result.risk_detail) else result.risk_detail,
        "trade_status": trade_status,
        "transaction_status": trade_status,
        "transaction_reason": result.transaction_decision_reason,
        "transaction_source": result.transaction_source,
        "tefas_traded_list_match": result.tefas_traded_list_match,
        "source_url": result.detail_url,
        "last_checked_at": result.test_time,
        "attempt_count": attempts,
        "data_quality": {
            "page_valid": page_valid,
            "start_year": "FOUND" if not start_missing else "SOURCE_NOT_FOUND",
            "risk_level": "FOUND" if not risk_missing else "SOURCE_NOT_FOUND",
            "trade_status": "FOUND" if trade_known else "UNRESOLVED",
            "failure_category": failure_category(result),
        },
    }


def diagnostics(
    funds_by_code: dict[str, FundEntry],
    progress: dict[str, FundResult],
    attempts: dict[str, int],
    *,
    max_field_attempts: int,
    max_technical_attempts: int,
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    retryable_codes: list[str] = []
    for code in sorted(funds_by_code):
        result = progress.get(code)
        count = attempts.get(code, 0)
        if result is None:
            category = "NOT_ATTEMPTED"
            retryable = True
            row = {
                "fund_code": code,
                "fund_name": funds_by_code[code].fund_name,
                "category": category,
                "attempt_count": count,
                "retryable": retryable,
                "error": "Henüz taranmadı.",
            }
        else:
            category = failure_category(result)
            retryable = (
                (needs_technical_retry(result) and count < max_technical_attempts)
                or (needs_field_retry(result) and count < max_field_attempts)
            )
            row = {
                "fund_code": code,
                "fund_name": result.fund_name,
                "category": category,
                "attempt_count": count,
                "retryable": retryable,
                "http_status": result.http_status,
                "page_code_verified": result.page_code_verified,
                "start_year": result.start_year,
                "risk_level": result.risk_level,
                "trade_status": result.transaction_status,
                "detail_url": result.detail_url,
                "error": result.error,
            }
        if category != "OK":
            rows.append(row)
            counts[category] = counts.get(category, 0) + 1
            if retryable:
                retryable_codes.append(code)
    return rows, dict(sorted(counts.items())), retryable_codes


def publish_if_ready(
    funds_by_code: dict[str, FundEntry],
    progress: dict[str, FundResult],
    attempts: dict[str, int],
    *,
    tefas_traded_count: int,
    pending_total: int,
) -> tuple[bool, dict[str, Any]]:
    total = len(funds_by_code)
    available = [progress[code] for code in funds_by_code if code in progress]
    valid_pages = sum(is_valid_page(item) for item in available)
    known_trade = sum(is_known_trade(item) for item in available)
    coverage_ratio = len(available) / total if total else 0.0
    valid_ratio = valid_pages / total if total else 0.0
    trade_ratio = known_trade / total if total else 0.0

    ready = bool(
        pending_total == 0
        and total >= MIN_EXPECTED_FUNDS
        and coverage_ratio == 1.0
        and valid_ratio >= MIN_VALID_PAGE_RATIO
        and trade_ratio >= MIN_KNOWN_TRADE_RATIO
    )
    metrics = {
        "total_kap_yf_count": total,
        "saved_result_count": len(available),
        "coverage_ratio": round(coverage_ratio, 6),
        "valid_page_count": valid_pages,
        "valid_page_ratio": round(valid_ratio, 6),
        "known_trade_count": known_trade,
        "known_trade_ratio": round(trade_ratio, 6),
        "tefas_traded_count": tefas_traded_count,
        "pending_total": pending_total,
    }
    if not ready:
        return False, metrics

    records = {
        code: public_record(progress[code], attempts.get(code, 0))
        for code in sorted(funds_by_code)
    }
    status_counts = {"AÇIK": 0, "KAPALI": 0, "KONTROL": 0}
    for record in records.values():
        status_counts[record["trade_status"]] += 1
    payload = {
        "schema_version": SCHEMA_VERSION,
        "publisher_version": PUBLISHER_VERSION,
        "source_engine_version": SOURCE_ENGINE_VERSION,
        "status": "SUCCESS" if status_counts["KONTROL"] == 0 else "SUCCESS_WITH_WARNINGS",
        "generated_at": utc_now_iso(),
        "universe": "KAP YF/Y active investment funds",
        "fund_count": total,
        "transaction_status_counts": status_counts,
        "quality_metrics": metrics,
        "sources": {
            "kap_active_list": "https://www.kap.org.tr/tr/api/fund/criteria/YF/Y",
            "kap_detail": "https://www.kap.org.tr/tr/fon-bilgileri/genel/{permalink}",
            "tefas_traded_list": "https://www.tefas.gov.tr/api/statistics/tefas/getFplFonList",
        },
        "rules": {
            "fund_name": "KAP aktif YF/Y ana listesindeki resmî fon adı",
            "start_year": "KAP Genel Bilgiler; eksikse Yatırımcı Bilgi Formu PDF",
            "risk_level": "KAP Genel Bilgiler; çoklu riskte en yüksek değer; eksikse PDF yedeği",
            "trade_status": "v9.1 Alım Satım Yerleri + TEFAS işlem listesi doğrulama kuralları",
        },
        "funds": records,
    }
    atomic_write_json(OFFICIAL_PATH, payload)
    return True, metrics


def run_batch(args: argparse.Namespace) -> dict[str, Any]:
    passed, total_rules = run_internal_rule_self_test()
    print(f"Kural öz testi: {passed}/{total_rules} BAŞARILI")

    funds, _ = fetch_kap_fund_list()
    funds_by_code = {fund.fund_code: fund for fund in funds}
    all_codes = sorted(funds_by_code)
    if len(all_codes) < MIN_EXPECTED_FUNDS:
        raise RuntimeError(f"KAP YF/Y fon sayısı olağan dışı düşük: {len(all_codes)}")

    tefas_traded: dict[str, dict[str, str]] = {}
    tefas_error = ""
    try:
        tefas_traded, _ = fetch_tefas_traded_funds()
    except Exception as exc:
        tefas_error = f"{type(exc).__name__}: {exc}"
        print(f"UYARI: TEFAS işlem listesi alınamadı: {tefas_error}")

    progress, attempts = load_progress()
    selected_codes, queue_counts_before = choose_batch(
        all_codes,
        progress,
        attempts,
        batch_size=args.batch_size,
        refresh_days=args.refresh_days,
        max_field_attempts=args.max_field_attempts,
        max_technical_attempts=args.max_technical_attempts,
    )

    print(
        f"KAP YF/Y: {len(all_codes)} | Kayıtlı: {len(progress)} | "
        f"Bu batch: {len(selected_codes)} | Bekleyen: {queue_counts_before['pending_total']}"
    )
    limiter = GlobalRateLimiter(
        args.delay,
        routine_request_limit=args.routine_request_limit,
        routine_cooldown_seconds=args.routine_cooldown_seconds,
    )

    for index, code in enumerate(selected_codes, start=1):
        fund = funds_by_code[code]
        current = test_one_fund(
            fund,
            {},
            tefas_traded,
            tefas_error,
            limiter,
            False,
        )
        attempts[code] = attempts.get(code, 0) + 1
        merged = merge_results(progress.get(code), current)
        progress[code] = merged
        append_attempt_event(current, attempts[code])
        save_progress(progress, attempts, total_funds=len(all_codes))
        print(
            f"[{index:>3}/{len(selected_codes)}] {code:<6} | "
            f"HTTP {current.http_status or '—'} | Başlangıç {merged.start_year} | "
            f"Risk {merged.risk_level} | İşlem {merged.transaction_status} | "
            f"Kategori {failure_category(current)}",
            flush=True,
        )

    failure_rows, failure_counts, retryable_codes = diagnostics(
        funds_by_code,
        progress,
        attempts,
        max_field_attempts=args.max_field_attempts,
        max_technical_attempts=args.max_technical_attempts,
    )
    atomic_write_json(FAILED_CODES_PATH, {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now_iso(),
        "count": len(retryable_codes),
        "codes": retryable_codes,
    })
    atomic_write_json(REQUEST_FAILURES_PATH, {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now_iso(),
        "count": len(failure_rows),
        "category_counts": failure_counts,
        "failures": failure_rows,
    })

    _, queue_counts_after = choose_batch(
        all_codes,
        progress,
        attempts,
        batch_size=args.batch_size,
        refresh_days=args.refresh_days,
        max_field_attempts=args.max_field_attempts,
        max_technical_attempts=args.max_technical_attempts,
    )
    published, quality = publish_if_ready(
        funds_by_code,
        progress,
        attempts,
        tefas_traded_count=len(tefas_traded),
        pending_total=queue_counts_after["pending_total"],
    )
    status = "PUBLISHED" if published else (
        "COMPLETE_WITH_UNRESOLVED" if queue_counts_after["pending_total"] == 0 else "IN_PROGRESS"
    )
    state = {
        "schema_version": SCHEMA_VERSION,
        "publisher_version": PUBLISHER_VERSION,
        "source_engine_version": SOURCE_ENGINE_VERSION,
        "updated_at": utc_now_iso(),
        "status": status,
        "batch_processed": len(selected_codes),
        "batch_size": args.batch_size,
        "saved_result_count": len(progress),
        "total_kap_yf_count": len(all_codes),
        "queue_before": queue_counts_before,
        "queue_after": queue_counts_after,
        "quality_metrics": quality,
        "official_file_updated": published,
        "next_action": (
            "Official JSON published." if published else
            "Run the workflow again; only pending, failed, incomplete or stale records will be selected."
        ),
    }
    atomic_write_json(RUN_STATE_PATH, state)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=int(os.getenv("KAP_BATCH_SIZE", DEFAULT_BATCH_SIZE)))
    parser.add_argument("--delay", type=float, default=float(os.getenv("KAP_DELAY_SECONDS", DEFAULT_DELAY_SECONDS)))
    parser.add_argument("--refresh-days", type=int, default=int(os.getenv("KAP_REFRESH_DAYS", DEFAULT_REFRESH_DAYS)))
    parser.add_argument("--max-field-attempts", type=int, default=DEFAULT_MAX_FIELD_ATTEMPTS)
    parser.add_argument("--max-technical-attempts", type=int, default=DEFAULT_MAX_TECHNICAL_ATTEMPTS)
    parser.add_argument("--routine-request-limit", type=int, default=65)
    parser.add_argument("--routine-cooldown-seconds", type=int, default=180)
    return parser.parse_args()


def main() -> int:
    run_batch(parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
