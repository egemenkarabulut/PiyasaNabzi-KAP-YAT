# -*- coding: utf-8 -*-

from pathlib import Path
import importlib.util
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_yat_kap_data.py"
spec = importlib.util.spec_from_file_location("yat_updater", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def page(code: str, start: str, risk: str, places: str) -> str:
    return f"""
    <html><body>
      <h1>TEST FONU {code}</h1>
      <table>
        <tr><th>İhraç Sıra Numarası</th><th>Fonun Halka Arz Tarihi</th></tr>
        <tr><td>1</td><td>{start}</td></tr>
      </table>
      <table>
        <tr>
          <th>Alım Satım Saatleri</th>
          <th>Alım Satım Yerleri</th>
          <th>Alınabilecek Asgari Pay Adedi</th>
        </tr>
        <tr><td>09:00</td><td>{places}</td><td>1</td></tr>
      </table>
      <table>
        <tr><th>Yatırım Stratejisi</th><th>Risk Değeri</th></tr>
        <tr><td>Test stratejisi</td><td>{risk}</td></tr>
      </table>
    </body></html>
    """


def test_tly_like_open():
    parsed = module.parse_kap_detail_html(
        page(
            "TLY",
            "11/03/2021",
            "7",
            "TERA PORTFÖY YÖNETİMİ A.Ş., TEFAS",
        ),
        "TLY",
    )
    assert parsed["start_year"] == "2021"
    assert parsed["risk_level"] == "7"
    assert parsed["transaction_status"] == "AÇIK"


def test_pbr_like_open():
    parsed = module.parse_kap_detail_html(
        page(
            "PBR",
            "27/03/2024",
            "5",
            "TEFAS'a üye fon dağıtım kuruluşları",
        ),
        "PBR",
    )
    assert parsed["start_year"] == "2024"
    assert parsed["risk_level"] == "5"
    assert parsed["transaction_status"] == "AÇIK"


def test_closed_when_places_exist_without_tefas():
    parsed = module.parse_kap_detail_html(
        page(
            "AII",
            "02/01/2019",
            "USD /3, TL/5",
            "Sadece Kurucu bünyesinde",
        ),
        "AII",
    )
    assert parsed["risk_level"] == "USD/3, TL/5"
    assert parsed["risk_values"] == [3, 5]
    assert parsed["transaction_status"] == "KAPALI"


def test_control_when_places_missing():
    parsed = module.parse_kap_detail_html(
        page(
            "ZZZ",
            "01/01/2020",
            "4",
            "Bilgi Mevcut Değil",
        ),
        "ZZZ",
    )
    assert parsed["transaction_status"] == "KONTROL"
