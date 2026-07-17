# Piyasa Nabzı Türkiye — YAT/KAP Merkezi

Bu repository, **KAP aktif YF/Y yatırım fonu evrenini** v9.3 kurallarıyla tarar ve public JSON veri beslemesi üretir.

## Yayınlanan ana alanlar

Her fon için:

- `fund_name`
- `start_year`
- `risk_level`
- `trade_status`

Geriye dönük uyumluluk için `transaction_status`, `trade_status` ile aynı değeri taşır.

## v9.3 kaynak kuralları

- Ana evren: KAP `YF/Y` aktif yatırım fonları.
- Fon adı: KAP ana listesindeki resmî ad.
- Başlangıç: KAP Genel Bilgiler; eksikse Yatırımcı Bilgi Formu PDF.
- Risk: KAP Genel Bilgiler; çoklu risk varsa tüm değer korunur ve ana değer olarak en yüksek risk kullanılır; eksikse PDF yedeği.
- İşlem durumu: KAP `Alım Satım Yerleri` alanı ve gerekli durumda güncel TEFAS işlem gören fon listesi birlikte değerlendirilir.
- Boş alan, sadece kurucu veya yalnız banka/portföy kanalları: `KAPALI`.
- Platformun tam adı/TEFDP ya da doğrulanmış TEFAS erişimi: `AÇIK`.
- Teknik erişim veya gerçek DOM ayrıştırma problemi: `KONTROL`/teşhis kuyruğu.


### v9.3 risk ayrıştırma sırası

1. `Yatırım Stratejisi` sütununun sağındaki `Risk Değeri` başlığı bulunur.
2. `rowspan`/`colspan` hesaba katılarak aynı sütunun alt satırındaki yalnız `1–7` değeri okunur.
3. Mevcut metin ve segment kuralları çapraz kontrol olarak uygulanır.
4. Güvenilir sonuç yoksa alan boş (`—`) bırakılır; tahmin yapılmaz.

## Hız ve kilitlenme koruması

- Varsayılan 1 işçi mantığı.
- KAP istekleri arasında minimum `1.35` saniye.
- Workflow 60 fonluk kalıcı batch'ler halinde çalışır.
- Her batch sonrasında staging ve diagnostics dosyaları GitHub'a commit edilir.
- Batch'ler arasında varsayılan 180 saniye soğuma vardır.
- HTTP 429 oluşursa v9.3 motoru 3 → 10 → 20 dakika kademeli bekler ve aynı noktadan devam eder.
- Geçici HTTP hatası daha önce doğrulanmış doğru alanların üzerine yazılmaz.

## Kalıcı dosyalar

```text
data/yat_fund_enrichment.json             # Resmî yayın; kalite eşiği geçince güncellenir
data/run_state.json                       # Çalışmanın mevcut durumu
data/staging/yat_kap_progress.json        # Her başarılı/başarısız denemenin checkpoint'i
data/staging/failed_codes.json            # Yeniden denenecek kodlar
data/diagnostics/request_failures.json    # Hata kategorileri ve ayrıntılar
data/diagnostics/attempt_events.jsonl     # Deneme geçmişi
```

`yat_fund_enrichment.json`, tarama yarımken veya kalite eşiği geçilmemişken değiştirilmez. Buna karşılık staging ve diagnostics her batch sonunda repoda kalıcı hale gelir.

## İlk çalıştırma

1. Paket içeriğini repository köküne yükleyin.
2. `Settings > Actions > General > Workflow permissions` altında **Read and write permissions** seçin.
3. `Actions > YAT KAP Merkezi Veri Güncelleme > Run workflow` yolunu açın.
4. İlk test için varsayılanları koruyun:
   - `batch_size: 60`
   - `max_batches: 40`
   - `cooldown_seconds: 180`
   - `delay_seconds: 1.35`
5. Workflow gece boyunca ilerler ve her batch'i ayrıca commit eder.

## Durumlar

- `IN_PROGRESS`: Kaldığı yerden devam edecek kayıtlar var.
- `PUBLISHED`: Resmî JSON kalite eşiğini geçti ve güncellendi.
- `COMPLETE_WITH_UNRESOLVED`: Kontrollü tekrar sınırı dolmuş birkaç kaynak problemi kaldı; diagnostics incelenmelidir.

## Public adres

```text
https://raw.githubusercontent.com/GITHUB_KULLANICI_ADI/REPO_ADI/main/data/yat_fund_enrichment.json
```


## Windows yerel tam tarama

`YEREL_TAM_TEST_BASLAT.bat` dosyasına çift tıklayın. Bu başlatıcı GitHub ile aynı `scripts/update_yat_kap_data.py` motorunu kullanır, 60 fonluk batch’lerle ilerler, her fondan sonra checkpoint kaydeder ve `PUBLISHED` durumunda otomatik durur. Eski bağımsız test `.bat` dosyasını bu repoya eklemeyin.
