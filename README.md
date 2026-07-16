# Piyasa Nabzı Türkiye — YAT/KAP Merkezi Referans Verisi

Bu public repo, **Fon Analiz Merkezi v9** için yalnızca üç küçük referans alanını
otomatik olarak üretir:

- `start_year` — başlangıç yılı
- `risk_level` — risk seviyesi
- `transaction_status` — TEFAS işlem durumu (`AÇIK` / `KAPALI`)

Fiyat, günlük getiri ve tarihsel performans verileri bu repoda tutulmaz.
Fon Analiz Merkezi'nin normal fon seçimi ve rapor oluşturma akışı değişmez.

## Kaynak düzeni

- Başlangıç yılı: KAP
- Risk seviyesi: KAP
- İşlem durumu: KAP
- Yalnızca Alım Satım Yerleri alanında çıplak `TEFAS` ifadesi varsa:
  güncel TEFAS İşlem Gören Yatırım Fonları listesiyle ikinci doğrulama

## Üretilen dosyalar

- `data/manifest.json` — uygulamanın önce kontrol edeceği küçük sürüm dosyası
- `data/yat_kap_current.json` — üç alanın güncel merkezi kaydı
- `data/errors.json` — eksik, korunmuş veya teknik sorunlu alanlar
- `data/changes.json` — son çalışmada değişen değerler

## Güvenlik kuralları

- Başarısız yeni istek, daha önce doğrulanmış değeri silmez.
- HTTP ve sayfa doğrulama oranı eşik altındaysa veri yayımlanmaz.
- İşlem durumu yeterli oranda AÇIK/KAPALI üretmezse veri yayımlanmaz.
- Kısmi `--limit` çalışması public veri dosyalarının üzerine yazamaz.
- Ham HTML ve PDF belgeleri repoya kaydedilmez.

## İlk kurulum

1. Bu paketin içeriğini `Piyasa_Nabzi_YAT_KAP_Public_GitHub_Repo`
   reposunun kök dizinine yükleyin.
2. GitHub'da **Actions** sekmesini açın.
3. **YAT KAP Merkezi Veri Güncelleme** iş akışını seçin.
4. **Run workflow** ile ilk tam çalışmayı manuel başlatın.
5. İş bittikten sonra `data/manifest.json` dosyasında
   `"status": "SUCCESS"` bulunduğunu kontrol edin.

İş akışı her gün Türkiye saatiyle **04:17**'de çalışır. Saat, şu dosyadan
değiştirilebilir:

`.github/workflows/update-yat-kap.yml`

## GitHub Actions yazma izni

Workflow dosyasında `permissions: contents: write` tanımlıdır. Repo ayarları
bunu engelliyorsa:

`Settings → Actions → General → Workflow permissions`

bölümünden yazma iznini açın.

## Sonraki aşama

İlk tam çalışma doğrulandıktan sonra Fon Analiz Merkezi v9'a şu açılış akışı
eklenecektir:

1. `manifest.json` kontrolü
2. Yeni sürüm varsa `yat_kap_current.json` indirme
3. SHA-256 ve şema doğrulaması
4. Yalnızca üç alanı yerel `fonpulse.db` içine güncelleme
5. GitHub erişilemezse son geçerli yerel değerlerle normal açılış
