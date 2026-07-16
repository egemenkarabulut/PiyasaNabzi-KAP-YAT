#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Piyasa Nabzı Türkiye — YAT/KAP merkezi referans veri güncelleyicisi.

Yalnızca üç uygulama alanını yayımlar:
- start_year
- risk_level
- transaction_status

Ana kaynak KAP'tır. Çıplak TEFAS ifadesi bulunan işlem kayıtlarında,
TEFAS'ın güncel "İşlem Gören Yatırım Fonları" listesi ikinci doğrulama
kaynağı olarak kullanılır.

Başarısız veya eksik yeni veri, daha önce doğrulanmış değeri silmez.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import kap_yat_source as source


SCHEMA_VERSION = "1.0"
PUBLISHER_VERSION = "1.0.0"

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT_DIR / "data"
RUN_OUTPUT_DIR = ROOT_DIR / ".run_output"

VALID_TRANSACTION_STATUSES = {"AÇIK", "KAPALI"}


def now_istanbul_iso() -> str:
    # GitHub runner UTC olabilir. Türkiye yıl boyunca UTC+03:00 kullanır.
    from datetime import timezone, timedelta
    tz = timezone(timedelta(hours=3))
    return datetime.now(tz).isoformat(timespec="seconds")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as handle:
        handle.write(text)
        temp_name = handle.name

    os.replace(temp_name, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_int(value: Any, minimum: int, maximum: int) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if minimum <= number <= maximum else None


def normalize_transaction(value: Any) -> str | None:
    normalized = source.normalize_status(value)
    return normalized if normalized in VALID_TRANSACTION_STATUSES else None


def valid_previous_funds(previous_payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(previous_payload, dict):
        return {}
    funds = previous_payload.get("funds", {})
    if not isinstance(funds, dict):
        return {}
    return {
        str(code).strip().upper(): row
        for code, row in funds.items()
        if isinstance(row, dict) and str(code).strip()
    }


def select_value(
    *,
    new_value: Any,
    previous_value: Any,
    validator,
) -> tuple[Any, str]:
    validated_new = validator(new_value)
    if validated_new is not None:
        return validated_new, "FRESH"

    validated_old = validator(previous_value)
    if validated_old is not None:
        return validated_old, "PREVIOUS_VALID"

    return None, "MISSING"


def build_public_record(
    result: source.FundResult,
    previous: dict[str, Any] | None,
    generated_at: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    previous = previous or {}
    issues: list[dict[str, Any]] = []

    start_year, start_state = select_value(
        new_value=result.start_year,
        previous_value=previous.get("start_year"),
        validator=lambda value: normalize_int(value, 1900, datetime.now().year + 1),
    )
    risk_level, risk_state = select_value(
        new_value=result.risk_level,
        previous_value=previous.get("risk_level"),
        validator=lambda value: normalize_int(value, 1, 7),
    )
    transaction_status, transaction_state = select_value(
        new_value=result.transaction_status,
        previous_value=previous.get("transaction_status"),
        validator=normalize_transaction,
    )

    if start_state != "FRESH":
        issues.append({
            "fund_code": result.fund_code,
            "field": "start_year",
            "state": start_state,
            "new_value": result.start_year,
            "error": result.error or result.fallback_error,
        })
    if risk_state != "FRESH":
        issues.append({
            "fund_code": result.fund_code,
            "field": "risk_level",
            "state": risk_state,
            "new_value": result.risk_level,
            "error": result.error or result.fallback_error,
        })
    if transaction_state != "FRESH":
        issues.append({
            "fund_code": result.fund_code,
            "field": "transaction_status",
            "state": transaction_state,
            "new_value": result.transaction_status,
            "error": result.error or result.fallback_error,
        })
    if result.error:
        issues.append({
            "fund_code": result.fund_code,
            "field": "technical",
            "state": "ERROR",
            "new_value": None,
            "error": result.error,
        })
    if result.transaction_conflict_flag == "EVET":
        issues.append({
            "fund_code": result.fund_code,
            "field": "transaction_status",
            "state": "CONFLICT",
            "new_value": result.transaction_status,
            "error": result.transaction_decision_reason,
        })

    source_updated_at = generated_at
    previous_source_updated = previous.get("source_updated_at")
    if all(state == "PREVIOUS_VALID" for state in (start_state, risk_state, transaction_state)):
        source_updated_at = previous_source_updated or generated_at

    record = {
        "fund_code": result.fund_code,
        "fund_name": result.fund_name,
        "start_year": start_year,
        "risk_level": risk_level,
        "transaction_status": transaction_status,
        "kap_permalink": result.fund_permalink,
        "source_updated_at": source_updated_at,
        "field_state": {
            "start_year": start_state,
            "risk_level": risk_state,
            "transaction_status": transaction_state,
        },
        "field_source": {
            "start_year": result.start_source if start_state == "FRESH" else previous.get("field_source", {}).get("start_year"),
            "risk_level": result.risk_source if risk_state == "FRESH" else previous.get("field_source", {}).get("risk_level"),
            "transaction_status": result.transaction_source if transaction_state == "FRESH" else previous.get("field_source", {}).get("transaction_status"),
        },
        "transaction_decision_reason": (
            result.transaction_decision_reason
            if transaction_state == "FRESH"
            else previous.get("transaction_decision_reason")
        ),
        "tefas_traded_list_match": (
            result.tefas_traded_list_match
            if transaction_state == "FRESH"
            else previous.get("tefas_traded_list_match")
        ),
    }
    return record, issues


def comparable_values(record: dict[str, Any] | None) -> dict[str, Any]:
    record = record or {}
    return {
        "start_year": record.get("start_year"),
        "risk_level": record.get("risk_level"),
        "transaction_status": record.get("transaction_status"),
    }


def build_changes(
    previous_funds: dict[str, dict[str, Any]],
    current_funds: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    all_codes = sorted(set(previous_funds) | set(current_funds))

    for code in all_codes:
        before = previous_funds.get(code)
        after = current_funds.get(code)

        if before is None:
            changes.append({
                "fund_code": code,
                "change_type": "ADDED",
                "before": None,
                "after": comparable_values(after),
            })
            continue

        if after is None:
            changes.append({
                "fund_code": code,
                "change_type": "REMOVED_FROM_ACTIVE_KAP_LIST",
                "before": comparable_values(before),
                "after": None,
            })
            continue

        before_values = comparable_values(before)
        after_values = comparable_values(after)
        if before_values != after_values:
            changes.append({
                "fund_code": code,
                "change_type": "UPDATED",
                "before": before_values,
                "after": after_values,
            })

    return changes


def validate_candidate(
    *,
    kap_count: int,
    results: list[source.FundResult],
    funds: dict[str, dict[str, Any]],
    self_test_passed: int,
    self_test_total: int,
    minimum_http_ratio: float,
    minimum_transaction_ratio: float,
) -> dict[str, Any]:
    total = max(1, len(results))
    http_success = sum(1 for item in results if item.http_status == 200)
    page_verified = sum(1 for item in results if item.page_code_verified == "EVET")
    transaction_known = sum(
        1 for item in funds.values()
        if item.get("transaction_status") in VALID_TRANSACTION_STATUSES
    )

    metrics = {
        "kap_active_fund_count": kap_count,
        "result_count": len(results),
        "published_fund_count": len(funds),
        "http_200_count": http_success,
        "http_200_ratio": round(http_success / total, 6),
        "page_verified_count": page_verified,
        "page_verified_ratio": round(page_verified / total, 6),
        "transaction_known_count": transaction_known,
        "transaction_known_ratio": round(transaction_known / max(1, len(funds)), 6),
        "rule_self_test": f"{self_test_passed}/{self_test_total}",
    }

    failures: list[str] = []
    if self_test_passed != self_test_total:
        failures.append("Kural öz testi başarısız.")
    if kap_count < 1500:
        failures.append(f"KAP aktif fon sayısı olağandışı düşük: {kap_count}")
    if len(results) != kap_count:
        failures.append(
            f"Sonuç sayısı KAP aktif fon sayısıyla eşleşmiyor: {len(results)} != {kap_count}"
        )
    if len(funds) != kap_count:
        failures.append(
            f"Yayımlanacak fon sayısı KAP aktif fon sayısıyla eşleşmiyor: {len(funds)} != {kap_count}"
        )
    if metrics["http_200_ratio"] < minimum_http_ratio:
        failures.append(
            f"HTTP başarı oranı eşik altında: {metrics['http_200_ratio']:.2%}"
        )
    if metrics["page_verified_ratio"] < minimum_http_ratio:
        failures.append(
            f"Sayfa kodu doğrulama oranı eşik altında: {metrics['page_verified_ratio']:.2%}"
        )
    if metrics["transaction_known_ratio"] < minimum_transaction_ratio:
        failures.append(
            "AÇIK/KAPALI işlem durumu oranı eşik altında: "
            f"{metrics['transaction_known_ratio']:.2%}"
        )

    return {
        "ok": not failures,
        "metrics": metrics,
        "failures": failures,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--delay", type=float, default=0.55)
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--minimum-http-ratio", type=float, default=0.80)
    parser.add_argument("--minimum-transaction-ratio", type=float, default=0.90)
    parser.add_argument("--limit", type=int, default=0, help="Yalnız yerel teşhis için.")
    parser.add_argument(
        "--save-diagnostics",
        action="store_true",
        help="Ham HTML/PDF teşhis dosyalarını .run_output altında saklar.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    RUN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.save_diagnostics:
        source.SAVE_DIAGNOSTIC_FILES = True

    source.OUTPUT_DIR = RUN_OUTPUT_DIR
    source.RAW_HTML_DIR = RUN_OUTPUT_DIR / "HAM_SAYFALAR"
    source.RAW_DOCUMENT_DIR = RUN_OUTPUT_DIR / "HAM_BELGELER"

    started = time.perf_counter()
    generated_at = now_istanbul_iso()

    print("=" * 88)
    print("PİYASA NABZI TÜRKİYE — YAT/KAP MERKEZİ VERİ GÜNCELLEME")
    print("=" * 88)
    print(f"Yayınlayıcı sürümü : {PUBLISHER_VERSION}")
    print(f"Kaynak motoru      : {source.SCRIPT_VERSION}")
    print(f"Veri klasörü       : {data_dir}")
    print(f"Çalışma zamanı     : {generated_at}")
    print("Kural öz testi çalıştırılıyor...")

    passed, total_self_tests = source.run_internal_rule_self_test()
    print(f"Kural öz testi     : {passed}/{total_self_tests}")

    print("KAP aktif YAT fon listesi indiriliyor...")
    kap_funds, _raw_kap_rows = source.fetch_kap_fund_list()
    if args.limit > 0:
        kap_funds = kap_funds[: args.limit]
        print("UYARI: --limit kullanıldı; bu çalışma public veri yayımlamak için değildir.")
    print(f"KAP aktif fon      : {len(kap_funds)}")

    print("TEFAS işlem gören yatırım fonları listesi indiriliyor...")
    tefas_error = ""
    tefas_funds: dict[str, dict[str, str]] = {}
    try:
        tefas_funds, _tefas_payload = source.fetch_tefas_traded_funds()
        print(f"TEFAS işlem gören  : {len(tefas_funds)}")
    except Exception as exc:
        tefas_error = f"{type(exc).__name__}: {exc}"
        print(f"TEFAS liste hatası : {tefas_error}")

    previous_payload = load_json(data_dir / "yat_kap_current.json", {})
    previous_funds = valid_previous_funds(previous_payload)

    workers = max(1, min(args.workers, 4))
    rate_limiter = source.GlobalRateLimiter(max(0.0, args.delay))
    results: list[source.FundResult] = []

    print(f"İşçi sayısı        : {workers}")
    print(f"İstek aralığı      : {max(0.0, args.delay):.2f} saniye")
    print("Fon detayları taranıyor...")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                source.test_one_fund,
                fund,
                {},
                tefas_funds,
                tefas_error,
                rate_limiter,
                False,
            ): fund
            for fund in kap_funds
        }

        completed = 0
        total_funds = len(kap_funds)
        for future in as_completed(future_map):
            result = future.result()
            results.append(result)
            completed += 1
            if completed <= 20 or completed % 50 == 0 or completed == total_funds:
                start = result.start_year
                risk = result.risk_level
                trade = result.transaction_status
                print(
                    f"[{completed:4d}/{total_funds}] {result.fund_code:<6} "
                    f"| Başlangıç {start:<4} | Risk {risk:<2} | İşlem {trade}"
                )

    results.sort(key=lambda item: item.fund_code)

    public_funds: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []

    for result in results:
        record, record_issues = build_public_record(
            result,
            previous_funds.get(result.fund_code),
            generated_at,
        )
        public_funds[result.fund_code] = record
        issues.extend(record_issues)

    changes = build_changes(previous_funds, public_funds)
    validation = validate_candidate(
        kap_count=len(kap_funds),
        results=results,
        funds=public_funds,
        self_test_passed=passed,
        self_test_total=total_self_tests,
        minimum_http_ratio=args.minimum_http_ratio,
        minimum_transaction_ratio=args.minimum_transaction_ratio,
    )

    elapsed_seconds = round(time.perf_counter() - started, 2)
    metrics = validation["metrics"]
    metrics.update({
        "start_year_known_count": sum(
            1 for row in public_funds.values() if row.get("start_year") is not None
        ),
        "risk_level_known_count": sum(
            1 for row in public_funds.values() if row.get("risk_level") is not None
        ),
        "transaction_open_count": sum(
            1 for row in public_funds.values() if row.get("transaction_status") == "AÇIK"
        ),
        "transaction_closed_count": sum(
            1 for row in public_funds.values() if row.get("transaction_status") == "KAPALI"
        ),
        "fresh_start_year_count": sum(
            1 for row in public_funds.values()
            if row.get("field_state", {}).get("start_year") == "FRESH"
        ),
        "fresh_risk_level_count": sum(
            1 for row in public_funds.values()
            if row.get("field_state", {}).get("risk_level") == "FRESH"
        ),
        "fresh_transaction_status_count": sum(
            1 for row in public_funds.values()
            if row.get("field_state", {}).get("transaction_status") == "FRESH"
        ),
        "issue_count": len(issues),
        "change_count": len(changes),
        "elapsed_seconds": elapsed_seconds,
    })

    if args.limit > 0:
        validation["ok"] = False
        validation["failures"].append(
            "--limit ile çalıştırılan kısmi tarama public veri olarak yayımlanmaz."
        )

    if not validation["ok"]:
        failed_payload = {
            "schema_version": SCHEMA_VERSION,
            "publisher_version": PUBLISHER_VERSION,
            "generated_at": generated_at,
            "status": "REJECTED",
            "metrics": metrics,
            "failures": validation["failures"],
            "issues": issues,
        }
        atomic_write_json(RUN_OUTPUT_DIR / "rejected_run.json", failed_payload)
        print("\nYAYIN REDDEDİLDİ:")
        for failure in validation["failures"]:
            print(f"- {failure}")
        print(f"Teşhis: {RUN_OUTPUT_DIR / 'rejected_run.json'}")
        return 2

    current_payload = {
        "schema_version": SCHEMA_VERSION,
        "publisher_version": PUBLISHER_VERSION,
        "generated_at": generated_at,
        "fund_count": len(public_funds),
        "funds": public_funds,
    }

    errors_payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "issue_count": len(issues),
        "issues": issues,
    }

    changes_payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "change_count": len(changes),
        "changes": changes,
    }

    current_path = data_dir / "yat_kap_current.json"
    errors_path = data_dir / "errors.json"
    changes_path = data_dir / "changes.json"

    atomic_write_json(current_path, current_payload)
    atomic_write_json(errors_path, errors_payload)
    atomic_write_json(changes_path, changes_payload)

    manifest_payload = {
        "schema_version": SCHEMA_VERSION,
        "publisher_version": PUBLISHER_VERSION,
        "generated_at": generated_at,
        "status": "SUCCESS",
        "data_file": "data/yat_kap_current.json",
        "data_sha256": sha256_file(current_path),
        "metrics": metrics,
        "sources": {
            "start_year": "KAP",
            "risk_level": "KAP",
            "transaction_status": "KAP; yalnız çıplak TEFAS ifadesinde TEFAS liste doğrulaması",
        },
    }
    atomic_write_json(data_dir / "manifest.json", manifest_payload)

    print("\nYAYIN BAŞARILI")
    print(f"Fon sayısı         : {len(public_funds)}")
    print(f"Başlangıç bulunan  : {metrics['start_year_known_count']}")
    print(f"Risk bulunan       : {metrics['risk_level_known_count']}")
    print(f"İşlem AÇIK         : {metrics['transaction_open_count']}")
    print(f"İşlem KAPALI       : {metrics['transaction_closed_count']}")
    print(f"Korunan eski değer : {sum(1 for row in public_funds.values() for state in row.get('field_state', {}).values() if state == 'PREVIOUS_VALID')}")
    print(f"Değişiklik         : {len(changes)}")
    print(f"Sorun kaydı        : {len(issues)}")
    print(f"Toplam süre        : {elapsed_seconds} saniye")
    print(f"Manifest           : {data_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
