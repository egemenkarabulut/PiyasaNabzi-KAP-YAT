# Piyasa Nabzı Türkiye — YAT/KAP Merkezi

Bu repository, **KAP aktif YF/Y yatırım fonu evrenini** v9.5 kurallarıyla tarar, eksik alanları kontrollü kaynak zinciriyle tamamlar, kalıcı checkpoint üzerinden kaldığı yerden devam eder ve kalite eşiği geçildiğinde public JSON veri beslemesi üretir.

## Büyük Resim — Veri Mimarisi

Sistem dört temel katmandan oluşur:

1. **Kaynak katmanı:** KAP aktif fon listesi, KAP Genel Bilgiler HTML, KAP Yatırımcı Bilgi Formu PDF ve yalnız gerekli olduğunda TEFAS JSON.
2. **Ayrıştırma ve doğrulama katmanı:** Fon adı, başlangıç yılı, risk seviyesi ve işlem durumu kuralları.
3. **Kalıcı çalışma katmanı:** Checkpoint, deneme sayıları, hata kayıtları ve fallback teşhisleri.
4. **Yayın katmanı:** Kalite kontrolü tamamlandıktan sonra güncellenen resmî JSON.

### Genel Akış Diyagramı

```mermaid
flowchart TD
    A["GitHub Actions veya yerel çalışma"] --> B["KAP aktif YF/Y fon evrenini al"]
    B --> C["Mevcut checkpoint'i yükle"]
    C --> D["İşlenecek batch'i seç"]
    D --> E["KAP Genel Bilgiler HTML"]
    E --> F["KAP Yatırımcı Bilgi Formu PDF fallback"]
    F --> G{"start_year hâlâ eksik mi?"}
    G -- "Hayır" --> H["Alan kalite kontrolü"]
    G -- "Evet" --> I["TEFAS 60 aylık JSON fallback"]
    I --> H
    H --> J["Checkpoint ve diagnostics yaz"]
    J --> K{"Kalite eşiği geçti mi?"}
    K -- "Hayır" --> L["IN_PROGRESS veya COMPLETE_WITH_UNRESOLVED"]
    K -- "Evet" --> M["data/yat_fund_enrichment.json yayımla"]
```

---

## Yayınlanan Ana Alanlar

Her fon için aşağıdaki ana alanlar yayımlanır:

| Alan | Anlamı | Birincil kaynak | Kontrollü yedek |
|---|---|---|---|
| `fund_name` | Resmî fon adı | KAP aktif YF/Y listesi | Mevcut doğrulanmış kayıt korunur |
| `start_year` | Fon başlangıç yılı | KAP HTML | KAP PDF → TEFAS 60 aylık JSON |
| `risk_level` | Risk seviyesi | KAP HTML | KAP PDF |
| `trade_status` | İşlem durumu | KAP Alım Satım Yerleri | Güncel TEFAS işlem doğrulaması |

Geriye dönük uyumluluk için `transaction_status`, `trade_status` ile aynı değeri taşır.

### Alan Veri Mimarisi

```mermaid
flowchart LR
    A["KAP aktif fon kodu"] --> B["fund_name"]
    A --> C["start_year"]
    A --> D["risk_level"]
    A --> E["trade_status"]

    C --> C1["KAP HTML"]
    C1 --> C2["KAP PDF"]
    C2 --> C3["TEFAS JSON 60 ay"]

    D --> D1["KAP HTML tablo/grid"]
    D1 --> D2["KAP PDF"]

    E --> E1["KAP Alım Satım Yerleri"]
    E1 --> E2["TEFAS işlem doğrulaması"]

    B --> F["Public JSON kaydı"]
    C --> F
    D --> F
    E --> F
```

### Alan Yayın Akışı

```mermaid
flowchart TD
    A["Ayrıştırılmış FundResult"] --> B{"Alan bulundu mu?"}
    B -- "Evet" --> C["FOUND"]
    B -- "Hayır" --> D["SOURCE_NOT_FOUND veya UNRESOLVED"]
    C --> E["public_record oluştur"]
    D --> E
    E --> F["trade_status bilinmiyorsa KONTROL"]
    F --> G["transaction_status ile uyumluluk alanı üret"]
    G --> H["Kalite kontrolüne gönder"]
```

---

## v9.5 Kaynak Önceliği

Genel kaynak önceliği, resmî ve doğrudan kaynakların yedek kaynaklardan önce kullanılmasını sağlar.

### Genel Kaynak Mimarisi

```mermaid
flowchart TD
    A["KAP aktif YF/Y ana listesi"] --> B["Fon kodu ve resmî fon adı"]
    B --> C["KAP Genel Bilgiler HTML"]
    C --> D["KAP Yatırımcı Bilgi Formu PDF"]
    D --> E{"Başlangıç yılı hâlâ eksik mi?"}
    E -- "Evet" --> F["TEFAS JSON 60 ay"]
    E -- "Hayır" --> G["KAP sonucu korunur"]
    F --> H["Yalnız start_year fallback sonucu"]
    G --> I["Birleştirilmiş fon kaydı"]
    H --> I
```

### Kaynak Üstünlüğü

```text
KAP aktif liste / KAP HTML
        >
KAP Yatırımcı Bilgi Formu PDF
        >
TEFAS 60 aylık JSON başlangıç yedeği
        >
Eksik bırakma; tahmin yapmama
```

---

### Fon Adı

#### Veri Mimarisi

Fon adı, KAP aktif `YF/Y` ana listesindeki resmî ad üzerinden alınır. Daha önce doğrulanmış dolu fon adı, geçici boş veya hatalı cevapla ezilmez.

```mermaid
flowchart TD
    A["KAP aktif YF/Y listesi"] --> B{"Fon kodu bulundu mu?"}
    B -- "Evet" --> C["Resmî fon adını al"]
    B -- "Hayır" --> D["Eski doğrulanmış kaydı koru"]
    C --> E{"Yeni ad dolu mu?"}
    E -- "Evet" --> F["fund_name güncelle"]
    E -- "Hayır" --> D
    D --> G["Checkpoint kaydı"]
    F --> G
```

---

### Başlangıç Tarihi / Yılı

#### Veri Mimarisi

Başlangıç tarihi kaynak zinciri:

1. KAP **Genel Bilgiler** görünür HTML alanları.
2. KAP **Yatırımcı Bilgi Formu PDF**.
3. İlk iki kaynak sonuç üretmezse TEFAS **60 aylık JSON fiyat serisi**.
4. Hiçbir kaynak güvenilir sonuç üretmezse alan boş bırakılır; tahmin yapılmaz.

KAP HTML/PDF kaynakları her zaman TEFAS yedeğinden üstündür. Mevcut KAP başlangıç tarihi TEFAS verisiyle ezilmez.

#### Başlangıç Tarihi Akış Diyagramı

```mermaid
flowchart TD
    A["Fon kodu"] --> B["KAP Genel Bilgiler HTML"]
    B --> C{"Doğrulanmış başlangıç etiketi ve tarih bulundu mu?"}
    C -- "Evet" --> D["KAP_HTML start_date/start_year"]
    C -- "Hayır" --> E["KAP Yatırımcı Bilgi Formu PDF"]
    E --> F{"İhraç/başlangıç tarihi bulundu mu?"}
    F -- "Evet" --> G["KAP_PDF start_date/start_year"]
    F -- "Hayır" --> H["TEFAS JSON periyod=60"]
    H --> I{"En eski geçerli tarih bulundu mu?"}
    I -- "Hayır" --> J["Eksik bırak ve diagnostics yaz"]
    I -- "Evet" --> K{"60 aylık sınır + 20 günden daha yeni mi?"}
    K -- "Hayır" --> L["TRUNCATED: yıl yazma"]
    K -- "Evet" --> M["TEFAS_FIRST_AVAILABLE_DATE_60M"]
    D --> N["Birleştirilmiş kayıt"]
    G --> N
    M --> N
```

#### KAP Başlangıç Etiketi Ailesi

Parser tek bir ifadeye bağlı değildir. Aşağıdaki doğrulanmış etiket ailesi ve normalleştirilmiş türevleri korunur:

- `Fonun Halka Arz Tarihi`
- `Fon Halka Arz Tarihi`
- `Halka Arz Tarihi`
- `Fonun Halka Arza Başlama Tarihi`
- `Fonun Satış Başlangıç Tarihi`
- `Fon Paylarının Satış Başlangıç Tarihi`
- `Payların Satış Başlangıç Tarihi`
- `İlk Satış Tarihi`
- `Satış Başlangıç Tarihi`
- `Fonun Kuruluş Tarihi`
- `Kuruluş Tarihi`
- `Fonun Başlangıç Tarihi`
- `Başlangıç Tarihi`
- `Fonun İlk İhraç Tarihi`
- `İlk İhraç Tarihi`
- `İhraç Tarihi`
- İngilizce KAP karşılıkları (`Public Offering Date`, `Inception Date`, `Issue Date` vb.)

Büyük/küçük harf, Türkçe karakter, boşluk, satır sonu, `:` işareti ve HTML hücre ayrımı normalleştirilir. Belge tarihi, rapor tarihi, güncelleme tarihi ve fiyat tarihi gibi ilgisiz tarihler başlangıç tarihi olarak kullanılmaz.

#### Etiket Ayrıştırma Akışı

```mermaid
flowchart TD
    A["HTML veya PDF metni"] --> B["Metni normalize et"]
    B --> C["Türkçe karakter / boşluk / satır sonu / iki nokta toleransı"]
    C --> D["Doğrulanmış etiket ailesini tara"]
    D --> E{"Yakınında geçerli tarih var mı?"}
    E -- "Hayır" --> F["Sonraki etiket veya fallback"]
    E -- "Evet" --> G{"İlgisiz tarih etiketi mi?"}
    G -- "Evet" --> F
    G -- "Hayır" --> H["start_date ve start_year üret"]
```

#### TEFAS 60 Aylık Başlangıç Yedeği

Endpoint:

```text
POST https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir
```

Payload:

```json
{"fonKodu":"IV7","dil":"TR","periyod":60}
```

Kesin kurallar:

1. Endpoint yalnız KAP HTML ve KAP PDF başlangıç tarihi bulamadığında çağrılır.
2. Fon başına yalnız **bir POST isteği** gönderilir.
3. `resultList` içindeki **en eski geçerli tarih** esas alınır.
4. İlk kaydın fiyatı `0` olsa bile tarih geçerlidir.
5. Fiyat yalnız teşhis amacıyla saklanır; tarih kabulünü engellemez.
6. En eski tarih, bugünden 60 ay önceki doğal sınırın `20 gün` çevresindeyse seri kırpılmış kabul edilir ve başlangıç yılı yazılmaz.
7. Kaynak etiketi `TEFAS_FIRST_AVAILABLE_DATE_60M` olur.
8. TEFAS WAF/HTTP/ağ hatasında mevcut KAP kaydı korunur ve kontrollü retry kuyruğu kullanılır.
9. Bir batch içinde WAF reddi görülürse aynı batch'te yeni TEFAS başlangıç isteği gönderilmez.
10. TEFAS başlangıç istekleri arasında rastgele `15–20 saniye` beklenir.

#### TEFAS Veri Akışı

```mermaid
flowchart TD
    A["start_year eksik fon"] --> B["Rate limiter izin kontrolü"]
    B --> C{"Batch daha önce WAF ile engellendi mi?"}
    C -- "Evet" --> D["BLOCKED_SKIPPED"]
    C -- "Hayır" --> E["15–20 saniye koruma aralığı"]
    E --> F["Tek POST isteği"]
    F --> G{"HTTP/JSON başarılı mı?"}
    G -- "Hayır" --> H["Retryable diagnostics"]
    G -- "Evet" --> I["resultList tarihlerini ayrıştır"]
    I --> J["En eski geçerli tarihi seç"]
    J --> K["Fiyatı yalnız teşhis için kaydet"]
    K --> L{"Tarih 60 ay sınırı + 20 günden yeni mi?"}
    L -- "Hayır" --> M["TRUNCATED; start_year boş"]
    L -- "Evet" --> N["start_year = ilk tarihin yılı"]
```

---

### Risk Seviyesi

#### Veri Mimarisi

Kaynak sırası:

1. KAP Genel Bilgiler görünür HTML.
2. Güvenilir sonuç yoksa KAP Yatırımcı Bilgi Formu PDF.

Çoklu risk değeri varsa tüm değerler korunur ve ana değer olarak en yüksek risk kullanılır.

#### Risk Ayrıştırma Akışı

```mermaid
flowchart TD
    A["KAP Genel Bilgiler HTML"] --> B["Risk Değeri başlığını bul"]
    B --> C["rowspan/colspan ile görsel tabloyu genişlet"]
    C --> D["Aynı sütundaki 1–7 değerlerini tara"]
    D --> E["Yatay tablo / dikey sütun / div-grid çapraz kontrolü"]
    E --> F{"Güvenilir risk bulundu mu?"}
    F -- "Evet" --> G["Tüm risk değerlerini koru"]
    G --> H["Ana risk = en yüksek değer"]
    F -- "Hayır" --> I["KAP PDF fallback"]
    I --> J{"PDF'de güvenilir 1–7 değeri bulundu mu?"}
    J -- "Evet" --> G
    J -- "Hayır" --> K["risk_level boş; tahmin yok"]
```

#### Risk Ayrıştırma Kuralları

1. `Yatırım Stratejisi` sütununun sağındaki `Risk Değeri` başlığı bulunur.
2. `rowspan`/`colspan` hesaba katılarak aynı sütunun alt satırındaki yalnız `1–7` değeri okunur.
3. Yatay tablo, dikey görsel sütun, div/grid, açık etiket ve sınırlandırılmış geniş bölüm yöntemleri çapraz kontrol edilir.
4. `TL`, `USD`, `EUR`, `A Grubu`, `B Grubu`, yüzde, ondalık ve `T+2` gibi yakın metinler risk adayı kabul edilmez.
5. Güvenilir sonuç yoksa alan boş (`—`) bırakılır; tahmin yapılmaz.

#### Yanlış Risk Adayı Engelleme Akışı

```mermaid
flowchart LR
    A["Yakın metin adayı"] --> B{"Tam sayı 1–7 mi?"}
    B -- "Hayır" --> X["Reddet"]
    B -- "Evet" --> C{"TL/USD/EUR veya grup etiketiyle ilişkili mi?"}
    C -- "Evet" --> X
    C -- "Hayır" --> D{"Yüzde, ondalık veya T+2 bağlamı mı?"}
    D -- "Evet" --> X
    D -- "Hayır" --> E["Risk adayı olarak kabul et"]
```

---

### İşlem Durumu

#### Veri Mimarisi

- KAP `Alım Satım Yerleri` alanı merkezlidir.
- Gerekli durumda güncel TEFAS işlem gören fon listesiyle doğrulanır.
- Boş alan, yalnız kurucu veya yalnız banka/portföy kanalları: `KAPALI`.
- Platformun tam adı/TEFDP veya doğrulanmış TEFAS erişimi: `AÇIK`.
- Teknik erişim ya da gerçek DOM ayrıştırma problemi: `KONTROL`/teşhis kuyruğu.

#### İşlem Durumu Akışı

```mermaid
flowchart TD
    A["KAP Alım Satım Yerleri"] --> B{"Alan güvenilir biçimde okundu mu?"}
    B -- "Hayır" --> C["KONTROL / teknik teşhis"]
    B -- "Evet" --> D{"TEFAS/TEFDP veya doğrulanmış platform erişimi var mı?"}
    D -- "Evet" --> E["AÇIK"]
    D -- "Hayır" --> F{"Alan boş ya da yalnız kurucu/banka/portföy kanalı mı?"}
    F -- "Evet" --> G["KAPALI"]
    F -- "Hayır" --> H["Güncel TEFAS işlem listesiyle doğrula"]
    H --> I{"İşlem görüyor mu?"}
    I -- "Evet" --> E
    I -- "Hayır" --> G
```

---

## Mevcut Kayıtları Koruma

v9.5 güncellemesi mevcut checkpoint ve resmî JSON'u sıfırlamaz.

- `data/staging/yat_kap_progress.json` aynen korunur.
- `data/yat_fund_enrichment.json` kalite eşiği geçilmeden değiştirilmez.
- Dolu ve doğrulanmış alan, yeni boş değerle ezilmez.
- KAP başlangıç tarihi, TEFAS başlangıç tarihiyle ezilmez.
- Geçici HTTP/WAF/ağ hatası daha önce doğrulanmış kaydın üzerine yazılmaz.
- Eski v9.4 parser ile eksik kalmış kayıtlar, deneme sayısı `3` veya daha yüksek olsa bile v9.5 motorunda **bir kez parser-upgrade kuyruğuna** alınır.
- v9.5 ile başarıyla işlenen kayıt aynı upgrade kuyruğuna tekrar girmez.

Bu nedenle GitHub workflow mevcut kayıtların kaldığı yerden devam eder; tüm fonları sıfırdan taramaz.

### Kayıt Koruma Mimarisi

```mermaid
flowchart TD
    A["Mevcut checkpoint kaydı"] --> B["Yeni tarama sonucu"]
    B --> C{"Yeni alan dolu ve güvenilir mi?"}
    C -- "Hayır" --> D["Eski dolu alanı koru"]
    C -- "Evet" --> E{"Kaynak önceliği daha yüksek mi?"}
    E -- "Hayır" --> D
    E -- "Evet" --> F["Alanı kontrollü güncelle"]
    D --> G["Atomic checkpoint yaz"]
    F --> G
    G --> H{"Kalite eşiği geçti mi?"}
    H -- "Hayır" --> I["Resmî JSON'u değiştirme"]
    H -- "Evet" --> J["Resmî JSON'u atomik yayımla"]
```

### Kaldığı Yerden Devam Akışı

```mermaid
flowchart TD
    A["Workflow başlar"] --> B["yat_kap_progress.json yüklenir"]
    B --> C["unattempted / technical / TEFAS retry / incomplete / parser upgrade / stale listeleri"]
    C --> D["Öncelikli batch seçilir"]
    D --> E["Her fon işlenir"]
    E --> F["Her sonuçtan sonra checkpoint güncellenir"]
    F --> G{"Batch tamamlandı mı?"}
    G -- "Hayır" --> E
    G -- "Evet" --> H["data/ commit ve push"]
    H --> I{"Run state tamamlandı mı?"}
    I -- "Hayır" --> J["Soğuma ve sonraki batch"]
    J --> D
    I -- "Evet" --> K["Döngüyü bitir"]
```

---

## Hız ve Kilitlenme Koruması

- Varsayılan 1 işçi mantığı.
- KAP istekleri arasında minimum `1.35` saniye.
- Workflow 60 fonluk kalıcı batch'ler hâlinde çalışır.
- Her batch sonrasında staging ve diagnostics dosyaları GitHub'a commit edilir.
- Batch'ler arasında varsayılan 180 saniye soğuma vardır.
- HTTP 429 oluşursa KAP motoru 3 → 10 → 20 dakika kademeli bekler ve aynı noktadan devam eder.
- TEFAS başlangıç JSON istekleri arasında rastgele 15–20 saniye bulunur.
- TEFAS WAF reddinde aynı batch içindeki sonraki TEFAS başlangıç istekleri durdurulur; KAP taraması ve checkpoint kaydı korunur.
- GitHub `concurrency` kilidi aynı veri güncelleme workflow'unun eşzamanlı çalışmasını engeller.

### Koruma Akış Diyagramı

```mermaid
flowchart TD
    A["Batch başlat"] --> B["KAP isteği"]
    B --> C{"HTTP 429 mu?"}
    C -- "Evet" --> D["3 → 10 → 20 dakika kademeli bekleme"]
    D --> B
    C -- "Hayır" --> E["Sonucu checkpoint'e yaz"]
    E --> F{"TEFAS fallback gerekli mi?"}
    F -- "Hayır" --> G["Sonraki fon"]
    F -- "Evet" --> H["15–20 saniye rate limit"]
    H --> I{"WAF reddi var mı?"}
    I -- "Evet" --> J["Bu batch'te TEFAS fallback'i sustur"]
    I -- "Hayır" --> K["Tek TEFAS POST"]
    J --> G
    K --> G
    G --> L{"Batch tamamlandı mı?"}
    L -- "Hayır" --> B
    L -- "Evet" --> M["Commit / push / 180 saniye soğuma"]
```

---

## Kalıcı Dosyalar

```text
data/yat_fund_enrichment.json                   # Resmî yayın; kalite eşiği geçince güncellenir
data/run_state.json                             # Çalışmanın mevcut durumu
data/staging/yat_kap_progress.json              # Her başarılı/başarısız denemenin checkpoint'i
data/staging/failed_codes.json                  # Yeniden denenecek kodlar
data/diagnostics/request_failures.json          # Hata kategorileri ve ayrıntılar
data/diagnostics/attempt_events.jsonl            # Genel deneme geçmişi
data/diagnostics/pdf_fallback_events.jsonl       # KAP PDF fallback geçmişi
data/diagnostics/tefas_start_year_events.jsonl   # TEFAS başlangıç fallback geçmişi
```

`yat_fund_enrichment.json`, tarama yarımken veya kalite eşiği geçilmemişken değiştirilmez. Staging ve diagnostics her batch sonunda repoda kalıcı hâle gelir.

### Dosya Veri Mimarisi

```mermaid
flowchart LR
    A["Tarama motoru"] --> B["data/staging/yat_kap_progress.json"]
    A --> C["data/diagnostics/attempt_events.jsonl"]
    A --> D["data/diagnostics/pdf_fallback_events.jsonl"]
    A --> E["data/diagnostics/tefas_start_year_events.jsonl"]
    A --> F["data/diagnostics/request_failures.json"]

    B --> G["data/staging/failed_codes.json"]
    B --> H["data/run_state.json"]
    H --> I{"Kalite eşiği geçti mi?"}
    I -- "Evet" --> J["data/yat_fund_enrichment.json"]
    I -- "Hayır" --> K["Mevcut resmî JSON korunur"]
```

### Dosya Yazma Akışı

```mermaid
flowchart TD
    A["Fon sonucu oluşur"] --> B["Attempt event ekle"]
    B --> C["PDF/TEFAS fallback event ekle"]
    C --> D["Checkpoint'i atomik yaz"]
    D --> E["Failed codes ve diagnostics üret"]
    E --> F["run_state güncelle"]
    F --> G{"PUBLISH koşulları sağlandı mı?"}
    G -- "Hayır" --> H["Staging ile devam"]
    G -- "Evet" --> I["Public JSON'u atomik yaz"]
```

---

## v9.5 Güncellemesini Mevcut Repoya Uygulama

Yalnız değişen/yeni dosyaları repository köküne aynı klasör yapısıyla yükleyin:

```text
.github/workflows/update-yat-kap-data.yml
scripts/kap_yat_source.py
scripts/update_yat_kap_data.py
scripts/tefas_start_year_source.py
tests/test_parser.py
README.md
CHANGES_v2.5_v9.5.md
```

**`data/` klasörünü silmeyin, değiştirmeyin veya eski paketle ezmeyin.** Böylece mevcut checkpoint korunur.

### Güncelleme Mimarisi

```mermaid
flowchart TD
    A["Sadece değişen dosyalar paketi"] --> B["Repository köküne aynı yollarla yükle"]
    B --> C["Kod / workflow / tests / README değişir"]
    B --> D["data/ klasörü değişmez"]
    D --> E["Mevcut checkpoint ve resmî JSON korunur"]
    C --> F["Commit"]
    E --> F
    F --> G["Run workflow"]
```

### Uygulama Akışı

1. GitHub'da değişen dosyaları commit edin.
2. `Actions > YAT KAP Merkezi Veri Güncelleme > Run workflow` yolunu açın.
3. İlk çalıştırmada varsayılanları koruyun:
   - `batch_size: 60`
   - `max_batches: 40`
   - `cooldown_seconds: 180`
   - `delay_seconds: 1.35`
   - `tefas_start_delay_min: 15`
   - `tefas_start_delay_max: 20`
4. Workflow mevcut checkpoint'i okuyup eski parser ile eksik kalmış kayıtları öncelikli upgrade kuyruğuna alır.
5. Her batch GitHub'a ayrıca commit edilir; yarıda kalsa sonraki çalışma kaldığı yerden devam eder.

```mermaid
flowchart TD
    A["Run workflow"] --> B["pytest -q"]
    B --> C{"Testler geçti mi?"}
    C -- "Hayır" --> D["Workflow durur; data değişmez"]
    C -- "Evet" --> E["Checkpoint yükle"]
    E --> F["60 fonluk batch seç"]
    F --> G["Tarama motorunu çalıştır"]
    G --> H["data/ değişikliklerini commit et"]
    H --> I["run_state oku"]
    I --> J{"PUBLISHED veya COMPLETE_WITH_UNRESOLVED mı?"}
    J -- "Evet" --> K["Workflow tamamlanır"]
    J -- "Hayır" --> L["180 saniye soğuma"]
    L --> F
```

---

## Durumlar

- `IN_PROGRESS`: Kaldığı yerden devam edecek kayıtlar var.
- `PUBLISHED`: Resmî JSON kalite eşiğini geçti ve güncellendi.
- `COMPLETE_WITH_UNRESOLVED`: Kontrollü tekrar sınırı dolmuş birkaç kaynak problemi kaldı; diagnostics incelenmelidir.

### Durum Akış Diyagramı

```mermaid
stateDiagram-v2
    [*] --> IN_PROGRESS
    IN_PROGRESS --> IN_PROGRESS: Bekleyen batch / retry var
    IN_PROGRESS --> PUBLISHED: Kalite eşiği geçti
    IN_PROGRESS --> COMPLETE_WITH_UNRESOLVED: Retry sınırı doldu ve çözülmeyen kayıt var
    PUBLISHED --> [*]
    COMPLETE_WITH_UNRESOLVED --> [*]
```

---

## Public Adres

```text
https://raw.githubusercontent.com/GITHUB_KULLANICI_ADI/REPO_ADI/main/data/yat_fund_enrichment.json
```

### Public Veri Akışı

```mermaid
flowchart LR
    A["KAP/TEFAS kaynakları"] --> B["Tarama ve doğrulama"]
    B --> C["Kalite kontrolü"]
    C --> D["data/yat_fund_enrichment.json"]
    D --> E["GitHub Raw URL"]
    E --> F["Dashboard / uygulama / rapor tüketicileri"]
```

Public URL yalnız `PUBLISHED` durumunda güncellenen resmî JSON'u sunar. Staging ve diagnostics dosyaları public veri sözleşmesinin parçası değildir.

---

## Windows Yerel Tam Tarama

`YEREL_TAM_TEST_BASLAT.bat`, GitHub ile aynı `scripts/update_yat_kap_data.py` motorunu kullanır. Mevcut data checkpoint'i korunarak devam eder. Eski bağımsız Playwright/Tesseract test paketleri ana repoya eklenmemelidir.

### Yerel Çalışma Mimarisi

```mermaid
flowchart TD
    A["YEREL_TAM_TEST_BASLAT.bat"] --> B["scripts/update_yat_kap_data.py"]
    B --> C["Aynı parser ve fallback kuralları"]
    C --> D["Mevcut data/staging checkpoint"]
    D --> E["Yerel batch taraması"]
    E --> F["Aynı run_state / diagnostics / public kalite kuralları"]
```

### Yerel ve GitHub Motor Uyumu

```mermaid
flowchart LR
    A["Windows yerel BAT"] --> C["update_yat_kap_data.py"]
    B["GitHub Actions workflow"] --> C
    C --> D["kap_yat_source.py"]
    C --> E["tefas_start_year_source.py"]
    C --> F["Kalıcı data dosyaları"]
```

Bu yapı sayesinde yerel testte doğrulanan kural ile GitHub workflow'da çalışan kural aynı kalır.
