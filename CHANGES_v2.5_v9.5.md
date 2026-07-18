# v2.5 / Parser v9.5 Değişiklikleri

## Eklenen

- `scripts/tefas_start_year_source.py`
- KAP HTML ve KAP YBF PDF sonuç üretmediğinde TEFAS 60 aylık JSON başlangıç fallback'i.
- `TEFAS_FIRST_AVAILABLE_DATE_60M` kaynak etiketi.
- TEFAS başlangıç istekleri için bağımsız 15–20 saniyelik rate limiter.
- WAF reddinden sonra aynı batch içindeki yeni TEFAS başlangıç isteklerini durdurma.
- `data/diagnostics/tefas_start_year_events.jsonl` teşhis kaydı.
- GitHub workflow girişlerinde TEFAS minimum/maksimum gecikme ayarları.

## Kesinleşen tarih kuralı

- `resultList` içindeki en eski geçerli tarih kullanılır.
- İlk fiyat `0` olsa bile tarih geçerlidir.
- Pozitif fiyat başlangıç şartı değildir.
- Fiyat yalnız teşhis verisidir.
- 60 aylık doğal sınır + 20 gün koruması uygulanır.
- Sınıra yakın tarih `TRUNCATED` kabul edilir ve yıl yazılmaz.

## Veri koruma

- Mevcut `data/staging/yat_kap_progress.json` formatı değiştirilmedi.
- `FundResult` veri modeli genişletilmedi; eski checkpoint doğrudan okunur.
- Mevcut dolu KAP/PDF başlangıç verisi TEFAS tarafından ezilmez.
- Geçici TEFAS hatası mevcut doğru alanları bozmaz.
- v9.4 ile eksik kalmış ve deneme sayısı 3 veya 4 olan kayıtlar v9.5'te bir kez yeniden seçilir.
- `data/` klasöründeki mevcut dosyalar patch paketine dahil edilmez.
