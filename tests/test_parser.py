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

profile_spec = importlib.util.spec_from_file_location("tefas_profile_source", SCRIPTS / "tefas_profile_source.py")
profile_source = importlib.util.module_from_spec(profile_spec)
assert profile_spec and profile_spec.loader
sys.modules[profile_spec.name] = profile_source
profile_spec.loader.exec_module(profile_source)


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
        "kap_transaction_status": "AÇIK",
        "kap_transaction_source": "KAP_DETAIL_HTML:Alım Satım Yerleri",
        "kap_transaction_evidence": "Test",
        "kap_transaction_confidence": "YÜKSEK",
        "tefas_profile_api_status": "API_OK",
        "tefas_profile_checked_at": "2026-07-19T01:00:00+00:00",
        "tefas_profile_attempt_count": 1,
        "error": "",
    })
    values.update(overrides)
    return source.FundResult(**values)


def test_v91_internal_rules_are_preserved():
    passed, total = source.run_internal_rule_self_test()
    assert passed == total == 9


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


def test_bpz_risk_is_read_from_matching_table_column():
    html = """
    <html><body>
      <h2>Fonun Yatırım Stratejisi ve Risk Değeri</h2>
      <table>
        <tr><th>Yatırım Stratejisi</th><th>Risk Değeri</th></tr>
        <tr>
          <td>Fon portföyünün tamamı devamlı olarak uzun bir strateji metninden oluşur.</td>
          <td>2</td>
        </tr>
      </table>
      <h2>Fon Karşılaştırma Ölçütü</h2>
    </body></html>
    """
    soup = source.clean_soup(html)
    result = source.extract_risk(soup, source.visible_lines(soup))
    assert result.value == "2"
    assert "YATAY+DİKEY YAPISAL" in result.source_label


def test_bpz_flattened_segment_fallback_reads_trailing_risk():
    html = """
    <html><body>
      <div>Fonun Yatırım Stratejisi ve Risk Değeri</div>
      <div>Yatırım Stratejisi Risk Değeri</div>
      <div>Fon portföyü uzun bir strateji metninden oluşacaktır.</div>
      <div>Yabancı para birimi cinsinden varlık dahil edilmeyecektir.2</div>
      <div>Fon Karşılaştırma Ölçütü</div>
    </body></html>
    """
    soup = source.clean_soup(html)
    result = source.extract_risk(soup, source.visible_lines(soup))
    assert result.value == "2"
    assert "GENİŞ BÖLÜM" in result.source_label


def test_segment_fallback_does_not_mistake_t_plus_two_for_risk():
    html = """
    <html><body>
      <div>Fonun Yatırım Stratejisi ve Risk Değeri</div>
      <div>Yatırım Stratejisi Risk Değeri</div>
      <div>İşlemler T+2 valörlüdür</div>
      <div>Fon Karşılaştırma Ölçütü</div>
    </body></html>
    """
    soup = source.clean_soup(html)
    result = source.extract_risk(soup, source.visible_lines(soup))
    assert result.value == "—"


def test_missing_risk_remains_dash_when_all_methods_fail():
    html = """
    <html><body>
      <div>Fonun Yatırım Stratejisi ve Risk Değeri</div>
      <div>Yatırım Stratejisi Risk Değeri</div>
      <div>Risk değeri henüz açıklanmamıştır.</div>
      <div>Fon Karşılaştırma Ölçütü</div>
    </body></html>
    """
    soup = source.clean_soup(html)
    result = source.extract_risk(soup, source.visible_lines(soup))
    assert result.value == "—"



def test_alc_visual_column_with_colspan_is_read_vertically():
    html = """
    <html><body><table>
      <tr><th colspan="5">Fonun Yatırım Stratejisi ve Risk Değeri</th></tr>
      <tr><th colspan="4">Yatırım Stratejisi</th><th>Risk Değeri</th></tr>
      <tr>
        <td colspan="4">Uzun strateji metni; %80, 2.4 ve başka rakamlar içerir.</td>
        <td><span>6</span></td>
      </tr>
    </table><h2>Fon Karşılaştırma Ölçütü</h2></body></html>
    """
    soup = source.clean_soup(html)
    result = source.extract_risk(soup, source.visible_lines(soup))
    assert result.value == "6"
    assert result.matched_scope == "YAPISAL_YATAY_DIKEY"
    assert "GÖRSEL SÜTUN" in result.evidence


def test_anl_visual_column_with_rowspan_is_read_vertically():
    html = """
    <html><body><table>
      <tr><th>Yatırım Stratejisi</th><th rowspan="1">Risk Değeri</th></tr>
      <tr><td>Yatırım stratejisi metni soldadır.</td><td>1</td></tr>
    </table><h2>Fon Karşılaştırma Ölçütü</h2></body></html>
    """
    soup = source.clean_soup(html)
    result = source.extract_risk(soup, source.visible_lines(soup))
    assert result.value == "1"
    assert result.matched_scope == "YAPISAL_YATAY_DIKEY"


def test_vertical_column_accepts_only_a_single_digit_1_to_7():
    invalid_values = ["Risk 5", "%2", "T+2", "2.4", "8", "12", ""]
    for invalid in invalid_values:
        html = f"""
        <html><body><table>
          <tr><th>Yatırım Stratejisi</th><th>Risk Değeri</th></tr>
          <tr><td>Strateji metni</td><td>{invalid}</td></tr>
        </table><h2>Fon Karşılaştırma Ölçütü</h2></body></html>
        """
        soup = source.clean_soup(html)
        result = source.extract_risk(soup, source.visible_lines(soup))
        assert result.value == "—", (invalid, result.value, result.evidence)


def test_alc_live_flattened_visible_layout_reads_trailing_six():
    html = """
    <html><body>
      <div>Fonun Yatırım Stratejisi ve Risk Değeri</div>
      <div>Yatırım Stratejisi Risk Değeri</div>
      <div>Yatırım stratejisinin; Fon toplam değerinin %80'i devamlı olarak BIST Temettü Endeksi'ne dahil yerli ihraççı paylarına yatırılacaktır.</div>
      <div>Fon'un hisse senedi yoğun fon olması nedeniyle Fon portföy değerinin en az %80'i devamlı olarak menkul kıymetlere yatırılır.</div>
      <div>Fon portföyüne yalnızca Türk Lirası cinsinden varlıklar ve işlemler dahil edilecektir.</div>
      <div>Fon'un portföyüne yabancı para birimi cinsinden varlık ve altın ile diğer kıymetli madenler dahil edilmeyecektir 6</div>
      <div>Fon Karşılaştırma Ölçütü</div>
    </body></html>
    """
    soup = source.clean_soup(html)
    result = source.extract_risk(soup, source.visible_lines(soup))
    assert result.value == "6"
    assert "GENİŞ BÖLÜM" in result.source_label
    assert result.confidence == "ÇOK YÜKSEK"


def test_currency_and_share_group_proximity_are_not_risk_candidates():
    samples = [
        "TL 6",
        "6 USD",
        "EUR yakınında 5",
        "A Grubu 4",
        "7 B Grubu",
        "pay grubu 3",
    ]
    for sample in samples:
        values, detail = source._risk_values_from_text(sample)
        assert values == [], (sample, values, detail)


def test_pdf_issue_date_accepts_space_after_colon():
    result = source.extract_start_from_investor_form("İhraç Tarihi: 06/11/2006")
    assert result.value == "2006"
    assert result.raw_value == "06/11/2006"
    assert "İhraç tarihi" in result.source_label


def test_pdf_issue_date_accepts_no_space_after_colon():
    result = source.extract_start_from_investor_form("İhraç Tarihi:06/11/2006")
    assert result.value == "2006"
    assert result.raw_value == "06/11/2006"


def test_parser_upgrade_requeues_old_incomplete_at_attempt_limit_once():
    old = make_result(
        risk_level="—",
        parse_method="v9.3-vertical-column-risk-1 | KAP_DETAIL_VISIBLE_HTML",
    )
    selected, counts = publisher.choose_batch(
        ["TST"], {"TST": old}, {"TST": 3},
        batch_size=60, refresh_days=6,
        max_field_attempts=3, max_technical_attempts=6,
    )
    assert selected == ["TST"]
    assert counts["parser_upgrade_retryable"] == 1

    new = make_result(
        risk_level="—",
        parse_method=f"{source.SCRIPT_VERSION} | KAP_DETAIL_VISIBLE_HTML",
    )
    selected, counts = publisher.choose_batch(
        ["TST"], {"TST": new}, {"TST": 4},
        batch_size=60, refresh_days=6,
        max_field_attempts=3, max_technical_attempts=6,
    )
    assert selected == []
    assert counts["parser_upgrade_retryable"] == 0


def test_div_grid_two_row_layout_reads_risk():
    html = """
    <html><body>
      <section>
        <div><span>Yatırım Stratejisi</span><span>Risk Değeri</span></div>
        <div><span>Uzun yatırım stratejisi metni</span><span>6</span></div>
      </section>
      <div>Fon Karşılaştırma Ölçütü</div>
    </body></html>
    """
    soup = source.clean_soup(html)
    values, evidence = source._extract_risk_from_div_grid_pairs(soup)
    assert values == [6]
    assert "DIV/GRID" in evidence

import tefas_start_year_source as tefas_start


def test_tefas_first_available_date_accepts_zero_price():
    payload = {
        "resultList": [
            {"tarih": "2023-06-23", "fiyat": 0},
            {"tarih": "2023-06-26", "fiyat": 0},
        ]
    }
    result = tefas_start.evaluate_tefas_start_year_payload(
        "KCN",
        payload,
        today=tefas_start.date(2026, 7, 19),
    )
    assert result.status == "ACCEPTED"
    assert result.start_date == "23.06.2023"
    assert result.start_year == "2023"
    assert result.first_available_price == "0"
    assert result.positive_price_count == 0
    assert result.source == "TEFAS_FIRST_AVAILABLE_DATE_60M"


def test_tefas_lts_uses_earliest_date_not_first_positive_date():
    payload = {
        "resultList": [
            {"tarih": "2023-08-22", "fiyat": 0},
            {"tarih": "2024-12-19", "fiyat": 0.999957},
        ]
    }
    result = tefas_start.evaluate_tefas_start_year_payload(
        "LTS",
        payload,
        today=tefas_start.date(2026, 7, 19),
    )
    assert result.start_date == "22.08.2023"
    assert result.start_year == "2023"
    assert result.first_positive_date == "19.12.2024"


def test_tefas_boundary_guard_rejects_truncated_60_month_series():
    payload = {
        "resultList": [
            {"tarih": "2021-07-25", "fiyat": 1.0},
            {"tarih": "2026-07-17", "fiyat": 2.0},
        ]
    }
    result = tefas_start.evaluate_tefas_start_year_payload(
        "OLD",
        payload,
        today=tefas_start.date(2026, 7, 19),
        tolerance_days=20,
    )
    assert result.status == "TRUNCATED"
    assert result.start_year == "—"
    assert result.source == "TEFAS_START_FALLBACK:TRUNCATED"


def test_old_v94_incomplete_record_at_attempt_four_is_requeued_for_v95():
    old = make_result(
        start_year="—",
        risk_level="5",
        parse_method="v9.4-multi-source-risk-start-1 | KAP_DETAIL_VISIBLE_HTML + KAP_YBF_PDF",
    )
    selected, counts = publisher.choose_batch(
        ["TST"], {"TST": old}, {"TST": 4},
        batch_size=60, refresh_days=6,
        max_field_attempts=3, max_technical_attempts=6,
    )
    assert selected == ["TST"]
    assert counts["parser_upgrade_retryable"] == 1


def test_v95_truncated_tefas_start_is_not_repeated_as_field_retry():
    current = make_result(
        start_year="—",
        risk_level="5",
        start_source="TEFAS_START_FALLBACK:TRUNCATED",
        parse_method=f"{source.SCRIPT_VERSION} | TEFAS_START_YEAR_JSON_60M",
    )
    selected, counts = publisher.choose_batch(
        ["TST"], {"TST": current}, {"TST": 5},
        batch_size=60, refresh_days=6,
        max_field_attempts=3, max_technical_attempts=6,
    )
    assert selected == []
    assert counts["tefas_start_retryable"] == 0
    assert counts["field_retryable"] == 0


def test_v95_waf_tefas_start_is_retryable_until_technical_limit():
    current = make_result(
        start_year="—",
        risk_level="5",
        start_source="TEFAS_START_FALLBACK:WAF_REJECTED",
        parse_method=f"{source.SCRIPT_VERSION} | TEFAS_START_YEAR_JSON_60M",
    )
    selected, counts = publisher.choose_batch(
        ["TST"], {"TST": current}, {"TST": 5},
        batch_size=60, refresh_days=6,
        max_field_attempts=3, max_technical_attempts=6,
    )
    assert selected == ["TST"]
    assert counts["tefas_start_retryable"] == 1


def test_test_one_fund_uses_tefas_only_after_kap_and_pdf_start_are_missing(monkeypatch):
    detail_html = """
    <html><body>
      <h1>TST TEST FONU</h1>
      <table>
        <tr><th>Yatırım Stratejisi</th><th>Risk Değeri</th></tr>
        <tr><td>Strateji metni</td><td>5</td></tr>
      </table>
      <table>
        <tr><th>Alım Satım Saatleri</th><th>Alım Satım Yerleri</th></tr>
        <tr><td>09:00</td><td>TEFAS</td></tr>
      </table>
    </body></html>
    """
    summary_html = "<html><body>Yatırımcı Bilgi Formu bağlantısı yok.</body></html>"

    class FakeResponse:
        def __init__(self, text, url):
            self.text = text
            self.url = url
            self.status_code = 200
            self.content = text.encode("utf-8")
            self.headers = {"Content-Type": "text/html; charset=utf-8"}
            self.encoding = "utf-8"
            self.apparent_encoding = "utf-8"

    responses = iter([
        (FakeResponse(detail_html, "https://example.test/detail"), 10),
        (FakeResponse(summary_html, "https://example.test/summary"), 8),
    ])
    monkeypatch.setattr(source, "request_with_retry", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(source, "thread_session", lambda: object())

    tefas_result = tefas_start.evaluate_tefas_start_year_payload(
        "TST",
        {"resultList": [{"tarih": "2023-06-23", "fiyat": 0}]},
        today=tefas_start.date(2026, 7, 19),
    )
    monkeypatch.setattr(source, "fetch_tefas_start_year", lambda *args, **kwargs: tefas_result)

    fund = source.FundEntry("TST", "TEST FONU", "1", "test-fonu", "ACTIVE", "YF")
    result = source.test_one_fund(
        fund,
        {},
        {"TST": {"durum": "AÇIK", "unvan": "TEST FONU", "tarih": "2026-07-19"}},
        "",
        source.GlobalRateLimiter(0),
        False,
        tefas_start.TefasStartYearRateLimiter(0, 0),
    )

    assert result.start_year == "2023"
    assert result.start_date == "23.06.2023"
    assert result.start_source == "TEFAS_FIRST_AVAILABLE_DATE_60M"
    assert "BAŞLANGIÇ-TEFAS-JSON" in result.fallback_used
    assert "TEFAS_START_YEAR_JSON_60M" in result.parse_method


def test_existing_kap_start_date_is_never_overwritten_by_tefas(monkeypatch):
    detail_html = """
    <html><body>
      <h1>TST TEST FONU</h1>
      <table><tr><th>Fonun Halka Arz Tarihi</th><td>23.06.2020</td></tr></table>
      <table>
        <tr><th>Yatırım Stratejisi</th><th>Risk Değeri</th></tr>
        <tr><td>Strateji metni</td><td>5</td></tr>
      </table>
      <table>
        <tr><th>Alım Satım Saatleri</th><th>Alım Satım Yerleri</th></tr>
        <tr><td>09:00</td><td>TEFAS</td></tr>
      </table>
    </body></html>
    """

    class FakeResponse:
        text = detail_html
        url = "https://example.test/detail"
        status_code = 200
        content = detail_html.encode("utf-8")
        headers = {"Content-Type": "text/html; charset=utf-8"}
        encoding = "utf-8"
        apparent_encoding = "utf-8"

    monkeypatch.setattr(source, "request_with_retry", lambda *args, **kwargs: (FakeResponse(), 10))
    monkeypatch.setattr(source, "thread_session", lambda: object())

    def forbidden_tefas_call(*args, **kwargs):
        raise AssertionError("KAP tarihi varken TEFAS çağrılmamalı")

    monkeypatch.setattr(source, "fetch_tefas_start_year", forbidden_tefas_call)
    fund = source.FundEntry("TST", "TEST FONU", "1", "test-fonu", "ACTIVE", "YF")
    result = source.test_one_fund(
        fund,
        {},
        {"TST": {"durum": "AÇIK"}},
        "",
        source.GlobalRateLimiter(0),
        False,
        tefas_start.TefasStartYearRateLimiter(0, 0),
    )
    assert result.start_year == "2020"
    assert result.start_source.startswith("KAP_DETAIL_HTML")
    assert "TEFAS_START_YEAR_JSON_60M" not in result.parse_method


def test_merge_keeps_existing_kap_start_over_new_tefas_start():
    previous = make_result(
        start_date="23/06/2020",
        start_year="2020",
        start_source="KAP_DETAIL_HTML:Fonun Halka Arz Tarihi",
    )
    current = make_result(
        start_date="23.06.2023",
        start_year="2023",
        start_source="TEFAS_FIRST_AVAILABLE_DATE_60M",
    )
    merged = source.merge_results(previous, current)
    assert merged.start_year == "2020"
    assert merged.start_source.startswith("KAP_DETAIL_HTML")


def test_merge_allows_kap_start_to_upgrade_previous_tefas_start():
    previous = make_result(
        start_date="23.06.2023",
        start_year="2023",
        start_source="TEFAS_FIRST_AVAILABLE_DATE_60M",
    )
    current = make_result(
        start_date="23/06/2020",
        start_year="2020",
        start_source="KAP_YBF_PDF:İhraç tarihi",
    )
    merged = source.merge_results(previous, current)
    assert merged.start_year == "2020"
    assert merged.start_source.startswith("KAP_YBF_PDF")


def test_v96_tefas_status_normalization_handles_turkish_unicode():
    assert profile_source.normalize_tefas_status("AKTİF") == "AÇIK"
    assert profile_source.normalize_tefas_status("PASİF") == "KAPALI"
    assert profile_source.normalize_tefas_status("TEFAS'ta işlem görüyor") == "AÇIK"
    assert profile_source.normalize_tefas_status("TEFAS'ta İşlem Görmüyor") == "KAPALI"


def test_v96_tly_profile_and_bulk_risk_are_confirmed():
    profile_payload = {
        "resultList": [{
            "fonKodu": "TLY",
            "fonUnvan": "TERA PORTFÖY BİRİNCİ SERBEST FON",
            "riskDegeri": "7",
            "tefasDurum": "TEFAS'ta işlem görüyor",
        }]
    }
    bulk_payload = {
        "resultList": [{
            "fonKodu": "TLY",
            "fonUnvan": "TERA PORTFÖY BİRİNCİ SERBEST FON",
            "riskDegeri": "7",
            "tefasDurum": True,
        }]
    }
    profile = profile_source.evaluate_profile_payload("TLY", profile_payload)
    bulk = profile_source.evaluate_bulk_payload(bulk_payload).rows["TLY"]
    decision = profile_source.resolve_risk(
        kap_risk="—",
        kap_source="KAP_DETAIL_HTML:Risk Değeri",
        kap_evidence="",
        kap_confidence="YOK",
        profile_risk_raw=profile.risk_raw,
        bulk_risk_raw=bulk.risk_raw,
    )
    assert profile.risk_value == 7
    assert bulk.risk_value == 7
    assert decision.final_value == "7"
    assert decision.tefas_comparison == "EŞLEŞİYOR"
    assert decision.conflict_flag == "HAYIR"


def test_v96_bck_null_risk_is_not_invented():
    payload = {
        "resultList": [{
            "fonKodu": "BCK",
            "riskDegeri": None,
            "tefasDurum": "TEFAS'ta işlem görüyor",
        }]
    }
    profile = profile_source.evaluate_profile_payload("BCK", payload)
    decision = profile_source.resolve_risk(
        kap_risk="—",
        kap_source="KAP_DETAIL_HTML:Risk Değeri",
        kap_evidence="",
        kap_confidence="YOK",
        profile_risk_raw=profile.risk_raw,
        bulk_risk_raw="",
    )
    assert profile.risk_value is None
    assert decision.final_value == "—"
    assert decision.tefas_comparison == "BOŞ"


def test_v96_bck_tefas_profile_overrides_conflicting_kap_trade_but_preserves_conflict():
    decision = profile_source.resolve_trade_status(
        kap_status="KAPALI",
        kap_source="KAP_DETAIL_HTML:Alım Satım Yerleri",
        kap_evidence="Sadece kurum kanalı",
        profile_status_raw="TEFAS'ta işlem görüyor",
        profile_status_normalized="AÇIK",
        traded_list_match="EVET",
        traded_list_status_raw="AKTİF",
    )
    assert decision.final_status == "AÇIK"
    assert decision.final_source == "TEFAS_PROFILE:tefasDurum + TEFAS:getFplFonList"
    assert decision.kap_comparison == "ÇATIŞMA"
    assert decision.conflict_flag == "EVET"
    assert decision.tefas_internal_conflict == "HAYIR"


def test_v96_old_checkpoint_row_migrates_without_data_loss():
    original = make_result(fund_code="OLD", transaction_status="KAPALI")
    row = source.asdict(original)
    for name in [
        "kap_transaction_status",
        "kap_transaction_source",
        "kap_transaction_evidence",
        "kap_transaction_confidence",
        "tefas_profile_checked_at",
        "tefas_profile_attempt_count",
        "tefas_profile_api_status",
        "tefas_profile_http_status",
        "tefas_profile_error",
        "tefas_profile_fund_name",
        "tefas_profile_isin",
        "tefas_profile_kap_link",
        "tefas_status_raw",
        "tefas_status_normalized",
        "tefas_bulk_status_raw",
        "tefas_bulk_status_normalized",
        "tefas_internal_conflict",
        "kap_tefas_status_comparison",
        "tefas_profile_risk_raw",
        "tefas_profile_risk",
        "tefas_bulk_risk_raw",
        "tefas_bulk_risk",
        "risk_tefas_comparison",
        "risk_conflict_flag",
    ]:
        row.pop(name, None)
    migrated = source.fund_result_from_dict(row)
    assert migrated.fund_code == "OLD"
    assert migrated.transaction_status == "KAPALI"
    assert migrated.kap_transaction_status == "KAPALI"
    assert migrated.tefas_profile_api_status == "NOT_CHECKED"


def test_v96_old_record_is_queued_once_for_tefas_profile_upgrade():
    old = make_result(
        fund_code="OLD",
        tefas_profile_api_status="NOT_CHECKED",
        tefas_profile_checked_at="",
        tefas_profile_attempt_count=0,
    )
    selected, counts = publisher.choose_batch(
        ["OLD"],
        {"OLD": old},
        {"OLD": 9},
        batch_size=60,
        refresh_days=6,
        max_field_attempts=3,
        max_technical_attempts=6,
        max_tefas_profile_attempts=3,
    )
    assert selected == ["OLD"]
    assert counts["tefas_profile_upgrade"] == 1
