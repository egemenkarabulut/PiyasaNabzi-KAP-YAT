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
