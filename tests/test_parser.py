# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

source_spec = importlib.util.spec_from_file_location("kap_yat_source", SCRIPTS / "kap_yat_source.py")
source = importlib.util.module_from_spec(source_spec)
assert source_spec and source_spec.loader
sys.modules[source_spec.name] = source
source_spec.loader.exec_module(source)

publisher_spec = importlib.util.spec_from_file_location("update_yat_kap_data", SCRIPTS / "update_yat_kap_data.py")
publisher = importlib.util.module_from_spec(publisher_spec)
assert publisher_spec and publisher_spec.loader
sys.modules[publisher_spec.name] = publisher
publisher_spec.loader.exec_module(publisher)


def make_result(**overrides):
    values = {}
    for field in fields(source.FundResult):
        if field.type in {"int | None", "int"}:
            values[field.name] = None
        else:
            values[field.name] = "—"
    values.update({
        "test_time": "2026-07-17T01:00:00+00:00",
        "fund_code": "TST",
        "fund_name": "TEST FONU",
        "detail_url": "https://example.test/tst",
        "summary_url": "https://example.test/tst-summary",
        "http_status": 200,
        "page_code_verified": "EVET",
        "start_year": "2020",
        "risk_level": "5",
        "risk_detail": "5",
        "transaction_status": "AÇIK",
        "transaction_decision_reason": "Test",
        "error": "",
    })
    values.update(overrides)
    return source.FundResult(**values)


def test_v91_internal_rules_are_preserved():
    passed, total = source.run_internal_rule_self_test()
    assert passed == total == 6


def test_empty_trade_place_is_closed_under_v91_rules():
    html = """
    <html><body><h1>ZZZ</h1><table>
      <tr><th>Alım Satım Saatleri</th><th>Alım Satım Yerleri</th><th>Nemalandırma</th></tr>
      <tr><td>09:00</td><td></td><td>-</td></tr>
    </table></body></html>
    """
    soup = source.clean_soup(html)
    result = source.extract_transaction(
        soup,
        source.visible_lines(soup),
        fund_code="ZZZ",
        tefas_traded_funds={},
        tefas_list_error="",
    )
    assert result.value == "KAPALI"


def test_bare_tefas_requires_current_list_match():
    html = """
    <html><body><h1>JET</h1><table>
      <tr><th>Alım Satım Saatleri</th><th>Alım Satım Yerleri</th><th>Nemalandırma</th></tr>
      <tr><td>09:00</td><td>TEFAS, banka şubeleri</td><td>-</td></tr>
    </table></body></html>
    """
    soup = source.clean_soup(html)
    opened = source.extract_transaction(
        soup, source.visible_lines(soup), fund_code="JET",
        tefas_traded_funds={"JET": {"durum": "AÇIK"}}, tefas_list_error="",
    )
    closed = source.extract_transaction(
        soup, source.visible_lines(soup), fund_code="ICF",
        tefas_traded_funds={}, tefas_list_error="",
    )
    assert opened.value == "AÇIK"
    assert closed.value == "KAPALI"


def test_merge_does_not_replace_verified_data_with_temporary_error():
    previous = make_result(start_year="2020", risk_level="5", transaction_status="AÇIK")
    current = make_result(
        http_status=None,
        page_code_verified="HAYIR",
        start_year="—",
        risk_level="—",
        transaction_status="BİLİNMİYOR",
        error="RuntimeError: HTTP_429",
    )
    merged = source.merge_results(previous, current)
    assert merged.start_year == "2020"
    assert merged.risk_level == "5"
    assert merged.transaction_status == "AÇIK"
    assert merged.error == ""


def test_batch_prioritizes_unattempted_before_incomplete():
    progress = {"AAA": make_result(fund_code="AAA", risk_level="—")}
    selected, counts = publisher.choose_batch(
        ["AAA", "BBB", "CCC"], progress, {"AAA": 1},
        batch_size=2, refresh_days=6,
        max_field_attempts=3, max_technical_attempts=6,
    )
    assert selected == ["BBB", "CCC"]
    assert counts["unattempted"] == 2


def test_official_publish_is_blocked_while_queue_has_pending_records(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "OFFICIAL_PATH", tmp_path / "official.json")
    funds = {f"F{i:04d}": source.FundEntry(f"F{i:04d}", f"Fon {i}", str(i), f"fon-{i}", "ACTIVE", "YF") for i in range(2001)}
    progress = {code: make_result(fund_code=code, fund_name=funds[code].fund_name) for code in funds}
    attempts = {code: 1 for code in funds}
    published, metrics = publisher.publish_if_ready(
        funds, progress, attempts, tefas_traded_count=1000, pending_total=1
    )
    assert published is False
    assert metrics["pending_total"] == 1
    assert not publisher.OFFICIAL_PATH.exists()
