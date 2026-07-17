# v2.0 — v9.1 GitHub entegrasyonu

- v9.1 akıllı bekleme ve ayrıştırma motoru eklendi.
- 8 işçi kaldırıldı; güvenli seri istek akışı kullanıldı.
- 60 fonluk kalıcı batch ve batch sonrası Git commit eklendi.
- HTTP 429 için 3/10/20 dakika kademeli bekleme eklendi.
- Checkpoint, failed-code ve request-failure dosyaları eklendi.
- Geçici hatanın önceki doğrulanmış veriyi ezmesi engellendi.
- `fund_name`, `start_year`, `risk_level`, `trade_status` resmî şemaya alındı.
- Resmî JSON ile staging verisi ayrıldı.
- PDF olmayan yanıtların pypdf ile açılması engellendi.
- GitHub job timeout 360 dakikaya çıkarıldı.
