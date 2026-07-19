#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Piyasa Nabzı Türkiye — KAP YF/Y resumable public-data publisher.

The extractor rules are imported from ``kap_yat_source.py`` (v9.6 KAP/PDF/TEFAS profile-risk-trade).
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
    fund_result_from_dict,
    merge_results,
    normalize_text,
    run_internal_rule_self_test,
    test_one_fund,
)
from tefas_profile_source import (
    PROFILE_RETRYABLE_STATUSES,
    TefasApiRateLimiter,
    TefasBulkFundRow,
    TefasProfileResult,
    fetch_tefas_bulk_snapshot,
    fetch_tefas_profile,
    resolve_risk,
    resolve_trade_status,
)

PUBLISHER_VERSION = "github-resumable-v2.6-v9.6"
SCHEMA_VERSION = 3

DATA_DIR = Path("data")
OFFICIAL_PATH = DATA_DIR / "yat_fund_enrichment.json"
PROGRESS_PATH = DATA_DIR / "staging" / "yat_kap_progress.json"
FAILED_CODES_PATH = DATA_DIR / "staging" / "failed_codes.json"
REQUEST_FAILURES_PATH = DATA_DIR / "diagnostics" / "request_failures.json"
RUN_STATE_PATH = DATA_DIR / "run_state.json"
ATTEMPT_EVENTS_PATH = DATA_DIR / "diagnostics" / "attempt_events.jsonl"
PDF_FALLBACK_EVENTS_PATH = DATA_DIR / "diagnostics" / "pdf_fallback_events.jsonl"
TEFAS_START_EVENTS_PATH = DATA_DIR / "diagnostics" / "tefas_start_year_events.jsonl"
TEFAS_PROFILE_EVENTS_PATH = DATA_DIR / "diagnostics" / "tefas_profile_events.jsonl"
RUN_OUTPUT_DIR = Path(".run_output") / "KAP_YAT_SOURCE"
TEFAS_PROFILE_RAW_DIR = RUN_OUTPUT_DIR / "TEFAS_PROFIL_JSON"
TEFAS_BULK_RAW_PATH = RUN_OUTPUT_DIR / "TEFAS_TOPLU_RISK" / "YAT_TOPLU_GETIRI_RISK_RAW.json"

DEFAULT_BATCH_SIZE = 60
DEFAULT_REFRESH_DAYS = 6
DEFAULT_MAX_FIELD_ATTEMPTS = 3
DEFAULT_MAX_TECHNICAL_ATTEMPTS = 6
DEFAULT_MAX_TEFAS_PROFILE_ATTEMPTS = 3
DEFAULT_TEFAS_START_DELAY_MIN = 15.0
DEFAULT_TEFAS_START_DELAY_MAX = 20.0
MIN_EXPECTED_FUNDS = 2000
MIN_VALID_PAGE_RATIO = 0.98
MIN_KNOWN_TRADE_RATIO = 0.98
MIN_TEFAS_PROFILE_RATIO = 0.98


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
    results: dict[str, FundResult] = {}
    if isinstance(rows, dict):
        for code, row in rows.items():
            if not isinstance(row, dict):
                continue
            try:
                results[str(code).upper()] = fund_result_from_dict(row)
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


def needs_tefas_start_retry(result: FundResult | None) -> bool:
    if not result or not is_valid_page(result) or not is_known_trade(result):
        return False
    if not is_missing(result.start_year):
        return False
    source = normalize_text(result.start_source).upper()
    return source in {
        "TEFAS_START_FALLBACK:WAF_REJECTED",
        "TEFAS_START_FALLBACK:REQUEST_ERROR",
        "TEFAS_START_FALLBACK:HTTP_ERROR",
        "TEFAS_START_FALLBACK:JSON_PARSE_ERROR",
        "TEFAS_START_FALLBACK:BLOCKED_SKIPPED",
    }


def start_field_can_retry(result: FundResult | None) -> bool:
    if not result or not is_missing(result.start_year):
        return False
    source = normalize_text(result.start_source).upper()
    if source in {
        "TEFAS_START_FALLBACK:TRUNCATED",
        "TEFAS_START_FALLBACK:EMPTY_RESULT",
        "TEFAS_START_FALLBACK:DATE_PARSE_ERROR",
    }:
        return False
    return not needs_tefas_start_retry(result)


def needs_field_retry(result: FundResult | None) -> bool:
    return bool(
        result
        and is_valid_page(result)
        and is_known_trade(result)
        and (start_field_can_retry(result) or is_missing(result.risk_level))
    )


def needs_parser_upgrade_retry(
    result: FundResult | None,
    attempt_count: int,
    max_field_attempts: int,
) -> bool:
    """Eski parser ile eksik kalmış kaydı v9.6 motorunda bir kez yeniden seçer."""
    return bool(
        result
        and needs_field_retry(result)
        and SOURCE_ENGINE_VERSION not in normalize_text(result.parse_method)
        and attempt_count >= max_field_attempts
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


def is_tefas_profile_complete(result: FundResult | None) -> bool:
    return bool(
        result
        and normalize_text(result.tefas_profile_api_status).upper() in {"API_OK", "PROFILE_EMPTY"}
        and normalize_text(result.tefas_profile_checked_at)
    )


def needs_tefas_profile_upgrade(result: FundResult | None) -> bool:
    if result is None:
        return False
    status = normalize_text(result.tefas_profile_api_status).upper()
    return status in {"", "—", "NOT_CHECKED"} or not normalize_text(result.tefas_profile_checked_at)


def needs_tefas_profile_retry(
    result: FundResult | None,
    max_tefas_profile_attempts: int,
) -> bool:
    if not result:
        return False
    status = normalize_text(result.tefas_profile_api_status).upper()
    return bool(
        status in PROFILE_RETRYABLE_STATUSES
        and int(result.tefas_profile_attempt_count or 0) < max_tefas_profile_attempts
    )


def choose_batch(
    all_codes: list[str],
    progress: dict[str, FundResult],
    attempts: dict[str, int],
    *,
    batch_size: int,
    refresh_days: int,
    max_field_attempts: int,
    max_technical_attempts: int,
    max_tefas_profile_attempts: int = DEFAULT_MAX_TEFAS_PROFILE_ATTEMPTS,
) -> tuple[list[str], dict[str, int]]:
    unattempted: list[str] = []
    technical: list[str] = []
    tefas_profile_retry: list[str] = []
    tefas_profile_upgrade: list[str] = []
    tefas_start_retry: list[str] = []
    incomplete: list[str] = []
    parser_upgrade: list[str] = []
    stale: list[str] = []

    for code in all_codes:
        result = progress.get(code)
        count = attempts.get(code, 0)
        if result is None:
            unattempted.append(code)
        elif needs_technical_retry(result) and count < max_technical_attempts:
            technical.append(code)
        elif needs_tefas_profile_retry(result, max_tefas_profile_attempts):
            tefas_profile_retry.append(code)
        elif needs_tefas_profile_upgrade(result):
            tefas_profile_upgrade.append(code)
        elif needs_tefas_start_retry(result) and count < max_technical_attempts:
            tefas_start_retry.append(code)
        elif needs_field_retry(result) and count < max_field_attempts:
            incomplete.append(code)
        elif needs_parser_upgrade_retry(result, count, max_field_attempts):
            parser_upgrade.append(code)
        elif is_stale(result, refresh_days):
            stale.append(code)

    ordered = (
        unattempted
        + technical
        + tefas_profile_retry
        + tefas_profile_upgrade
        + tefas_start_retry
        + incomplete
        + parser_upgrade
        + stale
    )
    selected = ordered[: max(1, batch_size)]
    return selected, {
        "unattempted": len(unattempted),
        "technical_retryable": len(technical),
        "tefas_profile_retryable": len(tefas_profile_retry),
        "tefas_profile_upgrade": len(tefas_profile_upgrade),
        "tefas_start_retryable": len(tefas_start_retry),
        "field_retryable": len(incomplete),
        "parser_upgrade_retryable": len(parser_upgrade),
        "stale": len(stale),
        "pending_total": len(ordered),
    }

def apply_tefas_enrichment(
    base: FundResult,
    profile: TefasProfileResult,
    bulk_row: TefasBulkFundRow | None,
    *,
    tefas_traded_row: dict[str, str] | None,
    tefas_list_error: str,
) -> FundResult:
    """KAP/PDF sonucuna TEFAS profil risk ve işlem doğrulamasını uygular."""
    data = asdict(base)

    kap_status = normalize_text(base.kap_transaction_status).upper()
    if kap_status not in {"AÇIK", "KAPALI", "BİLİNMİYOR"}:
        kap_status = normalize_text(base.transaction_status).upper() or "BİLİNMİYOR"
    if kap_status == "BİLİNMİYOR" and base.transaction_status in {"AÇIK", "KAPALI"}:
        kap_status = base.transaction_status

    kap_source = normalize_text(base.kap_transaction_source)
    if kap_source in {"", "—"}:
        kap_source = base.transaction_source
    kap_evidence = normalize_text(base.kap_transaction_evidence)
    if kap_evidence in {"", "—"}:
        kap_evidence = base.transaction_evidence
    kap_confidence = normalize_text(base.kap_transaction_confidence)
    if kap_confidence in {"", "—", "YOK"} and base.transaction_confidence:
        kap_confidence = base.transaction_confidence

    data["kap_transaction_status"] = kap_status
    data["kap_transaction_source"] = kap_source or "—"
    data["kap_transaction_evidence"] = kap_evidence or "—"
    data["kap_transaction_confidence"] = kap_confidence or "YOK"

    requested = profile.status != "BLOCKED_SKIPPED"
    data["tefas_profile_attempt_count"] = int(base.tefas_profile_attempt_count or 0) + (1 if requested else 0)
    data["tefas_profile_checked_at"] = profile.checked_at
    data["tefas_profile_api_status"] = profile.status
    data["tefas_profile_http_status"] = profile.http_status
    data["tefas_profile_error"] = profile.error
    data["tefas_profile_fund_name"] = profile.fund_name or "—"
    data["tefas_profile_isin"] = profile.isin_code or "—"
    data["tefas_profile_kap_link"] = profile.kap_link or "—"
    data["tefas_status_raw"] = profile.status_raw or "—"
    data["tefas_status_normalized"] = profile.status_normalized

    data["tefas_bulk_status_raw"] = (bulk_row.status_raw if bulk_row else "") or "—"
    data["tefas_bulk_status_normalized"] = (
        bulk_row.status_normalized if bulk_row else "KONTROL"
    )

    risk = resolve_risk(
        kap_risk=base.risk_level,
        kap_source=base.risk_source,
        kap_evidence=base.risk_evidence,
        kap_confidence=base.risk_confidence,
        profile_risk_raw=profile.risk_raw,
        bulk_risk_raw=(bulk_row.risk_raw if bulk_row else ""),
    )
    data["risk_level"] = risk.final_value
    if risk.final_source == normalize_text(base.risk_source) and risk.final_value not in {"", "—"}:
        data["risk_detail"] = base.risk_detail
        data["risk_multi_value"] = base.risk_multi_value
    else:
        data["risk_detail"] = risk.final_value if risk.final_value not in {"", "—"} else "—"
        data["risk_multi_value"] = "HAYIR"
    data["risk_source"] = risk.final_source
    data["risk_evidence"] = risk.final_evidence
    data["risk_confidence"] = risk.final_confidence
    data["tefas_profile_risk_raw"] = risk.profile_raw or "—"
    data["tefas_profile_risk"] = risk.profile_value or "—"
    data["tefas_bulk_risk_raw"] = risk.bulk_raw or "—"
    data["tefas_bulk_risk"] = risk.bulk_value or "—"
    data["risk_tefas_comparison"] = risk.tefas_comparison
    data["risk_conflict_flag"] = risk.conflict_flag

    traded_row = tefas_traded_row or {}
    list_match = (
        "HATA" if tefas_list_error else ("EVET" if tefas_traded_row else "HAYIR")
    )
    list_status_raw = normalize_text(traded_row.get("durum"))
    trade = resolve_trade_status(
        kap_status=kap_status,
        kap_source=kap_source,
        kap_evidence=kap_evidence,
        profile_status_raw=profile.status_raw,
        profile_status_normalized=profile.status_normalized,
        traded_list_match=list_match,
        traded_list_status_raw=list_status_raw,
        traded_list_error=tefas_list_error,
    )
    data["transaction_status"] = trade.final_status
    data["transaction_source"] = trade.final_source
    data["transaction_evidence"] = trade.final_evidence
    data["transaction_confidence"] = trade.final_confidence
    data["transaction_decision_reason"] = trade.final_reason
    data["transaction_conflict_flag"] = trade.conflict_flag
    data["tefas_internal_conflict"] = trade.tefas_internal_conflict
    data["kap_tefas_status_comparison"] = trade.kap_comparison
    data["tefas_traded_list_match"] = list_match
    data["tefas_traded_list_status"] = list_status_raw or ("HATA" if tefas_list_error else "—")
    data["tefas_traded_list_title"] = normalize_text(traded_row.get("unvan")) or "—"
    data["tefas_traded_list_date"] = normalize_text(traded_row.get("tarih")) or "—"

    parse_method = normalize_text(base.parse_method)
    if "TEFAS_PROFILE_JSON" not in parse_method:
        parse_method = normalize_text(parse_method + " + TEFAS_PROFILE_JSON + TEFAS_BULK_RISK_JSON")
    data["parse_method"] = parse_method
    return FundResult(**data)


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



def append_pdf_fallback_event(result: FundResult, attempt_count: int) -> None:
    if result.fallback_attempted != "EVET":
        return
    PDF_FALLBACK_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "time": utc_now_iso(),
        "fund_code": result.fund_code,
        "attempt_count": attempt_count,
        "investor_form_url_found": bool(normalize_text(result.investor_form_url)),
        "investor_form_url": result.investor_form_url,
        "pdf_http_status": result.investor_form_http_status,
        "fallback_used": result.fallback_used,
        "fallback_winner": result.fallback_winner,
        "fallback_error": result.fallback_error,
        "start_year": result.start_year,
        "start_source": result.start_source,
        "risk_level": result.risk_level,
        "risk_source": result.risk_source,
    }
    with PDF_FALLBACK_EVENTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")

def append_tefas_start_event(result: FundResult, attempt_count: int) -> None:
    source = normalize_text(result.start_source)
    parse_method = normalize_text(result.parse_method)
    if "TEFAS_START_YEAR_JSON_60M" not in parse_method and not source.startswith("TEFAS_"):
        return
    TEFAS_START_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "time": utc_now_iso(),
        "fund_code": result.fund_code,
        "attempt_count": attempt_count,
        "start_date": result.start_date,
        "start_year": result.start_year,
        "start_source": result.start_source,
        "start_confidence": result.start_confidence,
        "start_evidence": result.start_evidence,
        "fallback_used": result.fallback_used,
        "fallback_error": result.fallback_error,
    }
    with TEFAS_START_EVENTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def append_tefas_profile_event(result: FundResult, attempt_count: int) -> None:
    status = normalize_text(result.tefas_profile_api_status).upper()
    if status in {"", "—", "NOT_CHECKED"}:
        return
    TEFAS_PROFILE_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "time": utc_now_iso(),
        "fund_code": result.fund_code,
        "attempt_count": attempt_count,
        "profile_attempt_count": result.tefas_profile_attempt_count,
        "profile_api_status": result.tefas_profile_api_status,
        "profile_http_status": result.tefas_profile_http_status,
        "profile_error": result.tefas_profile_error,
        "tefas_status_raw": result.tefas_status_raw,
        "tefas_status_normalized": result.tefas_status_normalized,
        "tefas_traded_list_match": result.tefas_traded_list_match,
        "transaction_status": result.transaction_status,
        "kap_transaction_status": result.kap_transaction_status,
        "kap_tefas_status_comparison": result.kap_tefas_status_comparison,
        "tefas_internal_conflict": result.tefas_internal_conflict,
        "profile_risk_raw": result.tefas_profile_risk_raw,
        "bulk_risk_raw": result.tefas_bulk_risk_raw,
        "risk_level": result.risk_level,
        "risk_tefas_comparison": result.risk_tefas_comparison,
        "risk_conflict_flag": result.risk_conflict_flag,
    }
    with TEFAS_PROFILE_EVENTS_PATH.open("a", encoding="utf-8") as handle:
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
    profile_complete = is_tefas_profile_complete(result)
    return {
        "fund_code": result.fund_code,
        "fund_name": result.fund_name,
        "start_year": "" if start_missing else result.start_year,
        "start_source": result.start_source,
        "risk_level": "" if risk_missing else result.risk_level,
        "risk_detail": "" if is_missing(result.risk_detail) else result.risk_detail,
        "risk_source": result.risk_source,
        "risk_confidence": result.risk_confidence,
        "risk_conflict_flag": result.risk_conflict_flag,
        "risk_tefas_comparison": result.risk_tefas_comparison,
        "tefas_profile_risk_raw": "" if is_missing(result.tefas_profile_risk_raw) else result.tefas_profile_risk_raw,
        "tefas_bulk_risk_raw": "" if is_missing(result.tefas_bulk_risk_raw) else result.tefas_bulk_risk_raw,
        "trade_status": trade_status,
        "transaction_status": trade_status,
        "transaction_reason": result.transaction_decision_reason,
        "transaction_source": result.transaction_source,
        "transaction_evidence": result.transaction_evidence,
        "transaction_confidence": result.transaction_confidence,
        "transaction_conflict_flag": result.transaction_conflict_flag,
        "kap_transaction_status": result.kap_transaction_status,
        "kap_transaction_source": result.kap_transaction_source,
        "kap_tefas_status_comparison": result.kap_tefas_status_comparison,
        "tefas_status_raw": "" if is_missing(result.tefas_status_raw) else result.tefas_status_raw,
        "tefas_status_normalized": result.tefas_status_normalized,
        "tefas_internal_conflict": result.tefas_internal_conflict,
        "tefas_traded_list_match": result.tefas_traded_list_match,
        "tefas_traded_list_status": result.tefas_traded_list_status,
        "tefas_profile_checked_at": result.tefas_profile_checked_at,
        "tefas_profile_api_status": result.tefas_profile_api_status,
        "tefas_profile_isin": "" if is_missing(result.tefas_profile_isin) else result.tefas_profile_isin,
        "tefas_profile_kap_link": "" if is_missing(result.tefas_profile_kap_link) else result.tefas_profile_kap_link,
        "source_url": result.detail_url,
        "last_checked_at": result.test_time,
        "attempt_count": attempts,
        "data_quality": {
            "page_valid": page_valid,
            "start_year": "FOUND" if not start_missing else "SOURCE_NOT_FOUND",
            "risk_level": "FOUND" if not risk_missing else "SOURCE_NOT_PUBLISHED",
            "trade_status": "FOUND" if trade_known else "UNRESOLVED",
            "tefas_profile": "CHECKED" if profile_complete else "PENDING_OR_ERROR",
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
    max_tefas_profile_attempts: int,
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
                or needs_tefas_profile_upgrade(result)
                or needs_tefas_profile_retry(result, max_tefas_profile_attempts)
                or (needs_tefas_start_retry(result) and count < max_technical_attempts)
                or (needs_field_retry(result) and count < max_field_attempts)
                or needs_parser_upgrade_retry(result, count, max_field_attempts)
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
                "start_source": result.start_source,
                "risk_level": result.risk_level,
                "risk_source": result.risk_source,
                "risk_conflict_flag": result.risk_conflict_flag,
                "risk_tefas_comparison": result.risk_tefas_comparison,
                "trade_status": result.transaction_status,
                "kap_transaction_status": result.kap_transaction_status,
                "kap_tefas_status_comparison": result.kap_tefas_status_comparison,
                "transaction_conflict_flag": result.transaction_conflict_flag,
                "tefas_status_raw": result.tefas_status_raw,
                "tefas_status_normalized": result.tefas_status_normalized,
                "tefas_profile_api_status": result.tefas_profile_api_status,
                "tefas_profile_attempt_count": result.tefas_profile_attempt_count,
                "tefas_profile_error": result.tefas_profile_error,
                "fallback_attempted": result.fallback_attempted,
                "investor_form_url": result.investor_form_url,
                "investor_form_http_status": result.investor_form_http_status,
                "fallback_used": result.fallback_used,
                "fallback_error": result.fallback_error,
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
    tefas_profile_checked = sum(is_tefas_profile_complete(item) for item in available)
    coverage_ratio = len(available) / total if total else 0.0
    valid_ratio = valid_pages / total if total else 0.0
    trade_ratio = known_trade / total if total else 0.0
    tefas_profile_ratio = tefas_profile_checked / total if total else 0.0

    ready = bool(
        pending_total == 0
        and total >= MIN_EXPECTED_FUNDS
        and coverage_ratio == 1.0
        and valid_ratio >= MIN_VALID_PAGE_RATIO
        and trade_ratio >= MIN_KNOWN_TRADE_RATIO
        and tefas_profile_ratio >= MIN_TEFAS_PROFILE_RATIO
    )
    metrics = {
        "total_kap_yf_count": total,
        "saved_result_count": len(available),
        "coverage_ratio": round(coverage_ratio, 6),
        "valid_page_count": valid_pages,
        "valid_page_ratio": round(valid_ratio, 6),
        "known_trade_count": known_trade,
        "known_trade_ratio": round(trade_ratio, 6),
        "tefas_profile_checked_count": tefas_profile_checked,
        "tefas_profile_checked_ratio": round(tefas_profile_ratio, 6),
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
            "tefas_profile": "https://www.tefas.gov.tr/api/funds/fonProfilBilgiGetir",
            "tefas_bulk_risk": "https://www.tefas.gov.tr/api/funds/fonGetiriBazliBilgiGetir",
            "tefas_traded_list": "https://www.tefas.gov.tr/api/statistics/tefas/getFplFonList",
            "tefas_start_year": "https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir",
        },
        "rules": {
            "fund_name": "KAP aktif YF/Y ana listesindeki resmî fon adı",
            "start_year": "KAP Genel Bilgiler > KAP Yatırımcı Bilgi Formu PDF > TEFAS 60 ay JSON en eski geçerli tarih; fiyat 0 olabilir; 20 gün sınır koruması",
            "risk_level": "KAP HTML > KAP PDF > TEFAS toplu riskDegeri > TEFAS profil riskDegeri; yalnız doğrulanmış 1-7 kabul; null/boş/- eksik bırakılır",
            "trade_status": "TEFAS profil tefasDurum birincil nihai TEFAS kaynağı; getFplFonList doğrulama; KAP çatışması ve önceki kanıt ayrıca saklanır",
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
        max_tefas_profile_attempts=args.max_tefas_profile_attempts,
    )

    print(
        f"KAP YF/Y: {len(all_codes)} | Kayıtlı: {len(progress)} | "
        f"Bu batch: {len(selected_codes)} | Bekleyen: {queue_counts_before['pending_total']}"
    )

    bulk_snapshot = None
    if selected_codes:
        bulk_snapshot = fetch_tefas_bulk_snapshot(raw_path=TEFAS_BULK_RAW_PATH)
        print(
            f"TEFAS toplu risk: {bulk_snapshot.status} | "
            f"HTTP {bulk_snapshot.http_status or '—'} | Satır {bulk_snapshot.row_count}"
        )
    bulk_rows = bulk_snapshot.rows if bulk_snapshot is not None else {}

    kap_limiter = GlobalRateLimiter(
        args.delay,
        routine_request_limit=args.routine_request_limit,
        routine_cooldown_seconds=args.routine_cooldown_seconds,
    )
    # Profil ve 60 aylık başlangıç POST'ları aynı güvenli TEFAS sırasını paylaşır.
    tefas_api_limiter = TefasApiRateLimiter(
        args.tefas_start_delay_min,
        args.tefas_start_delay_max,
    )

    for index, code in enumerate(selected_codes, start=1):
        fund = funds_by_code[code]
        profile = fetch_tefas_profile(
            code,
            rate_limiter=tefas_api_limiter,
            raw_path=TEFAS_PROFILE_RAW_DIR / f"{code}_FON_PROFIL_RAW.json",
        )
        current = test_one_fund(
            fund,
            {},
            tefas_traded,
            tefas_error,
            kap_limiter,
            False,
            tefas_api_limiter,
        )
        attempts[code] = attempts.get(code, 0) + 1
        base = merge_results(progress.get(code), current)
        enriched = apply_tefas_enrichment(
            base,
            profile,
            bulk_rows.get(code),
            tefas_traded_row=tefas_traded.get(code),
            tefas_list_error=tefas_error,
        )
        progress[code] = enriched
        append_attempt_event(enriched, attempts[code])
        append_pdf_fallback_event(enriched, attempts[code])
        append_tefas_start_event(enriched, attempts[code])
        append_tefas_profile_event(enriched, attempts[code])
        save_progress(progress, attempts, total_funds=len(all_codes))
        print(
            f"[{index:>3}/{len(selected_codes)}] {code:<6} | "
            f"KAP HTTP {current.http_status or '—'} | Profil {profile.status}/"
            f"{profile.http_status or '—'} | Başlangıç {enriched.start_year} | "
            f"Risk {enriched.risk_level} ({enriched.risk_source}) | "
            f"İşlem {enriched.transaction_status} | "
            f"KAP↔TEFAS {enriched.kap_tefas_status_comparison} | "
            f"Kategori {failure_category(enriched)}",
            flush=True,
        )

    failure_rows, failure_counts, retryable_codes = diagnostics(
        funds_by_code,
        progress,
        attempts,
        max_field_attempts=args.max_field_attempts,
        max_technical_attempts=args.max_technical_attempts,
        max_tefas_profile_attempts=args.max_tefas_profile_attempts,
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
        max_tefas_profile_attempts=args.max_tefas_profile_attempts,
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
        "tefas_bulk_status": bulk_snapshot.status if bulk_snapshot else "NOT_NEEDED",
        "tefas_bulk_row_count": bulk_snapshot.row_count if bulk_snapshot else 0,
        "queue_before": queue_counts_before,
        "queue_after": queue_counts_after,
        "quality_metrics": quality,
        "official_file_updated": published,
        "next_action": (
            "Official JSON published." if published else
            "Run the workflow again; only pending, failed, incomplete, profile-upgrade or stale records will be selected."
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
    parser.add_argument(
        "--max-tefas-profile-attempts",
        type=int,
        default=DEFAULT_MAX_TEFAS_PROFILE_ATTEMPTS,
    )
    parser.add_argument("--routine-request-limit", type=int, default=65)
    parser.add_argument("--routine-cooldown-seconds", type=int, default=180)
    parser.add_argument(
        "--tefas-start-delay-min",
        type=float,
        default=float(os.getenv("TEFAS_START_DELAY_MIN", DEFAULT_TEFAS_START_DELAY_MIN)),
    )
    parser.add_argument(
        "--tefas-start-delay-max",
        type=float,
        default=float(os.getenv("TEFAS_START_DELAY_MAX", DEFAULT_TEFAS_START_DELAY_MAX)),
    )
    return parser.parse_args()


def main() -> int:
    run_batch(parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
