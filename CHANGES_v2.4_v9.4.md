# v2.4 / v9.4

- ALC tipi risk düzeni için yatay, dikey, div/grid, geniş bölüm ve son-segment kontrolleri bağımsız çalışır.
- Yapısal kontrol yalnız başlık satırı ile hemen altındaki görsel veri satırını okur.
- TL/USD/EUR ve A/B/C/D/pay grubu yakınlığındaki rakamlar risk adayı olmaktan çıkarıldı.
- PDF başlangıç tarihi için `İhraç Tarihi: gg/mm/yyyy` ve `İhraç Tarihi:gg/mm/yyyy` biçimleri öncelikli desteklenir.
- PDF fallback olayları ayrıntılı JSONL teşhisine yazılır ve ham HTML/PDF çıktıları Actions artifact olarak saklanır.
- v9.3 ile deneme sınırına ulaşıp eksik kalan kayıtlar v9.4 parserıyla bir kez yeniden kuyruğa alınır.
