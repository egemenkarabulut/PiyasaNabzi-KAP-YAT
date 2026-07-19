# v2.6 / v9.6 Değişiklikleri

## TEFAS profil entegrasyonu

Yeni kaynak:

```text
POST https://www.tefas.gov.tr/api/funds/fonProfilBilgiGetir
```

Kaydedilen alanlar:

- `riskDegeri`
- `tefasDurum`
- `isinKodu`
- `kapLink`
- API durumu, HTTP kodu, hata ve kontrol zamanı

## TEFAS toplu risk entegrasyonu

Yeni kaynak:

```text
POST https://www.tefas.gov.tr/api/funds/fonGetiriBazliBilgiGetir
```

Her batch başında yalnız bir kez çağrılır. Fon bazında `riskDegeri` ve toplu `tefasDurum` teşhisi saklanır.

## Risk modeli

Kaynak önceliği:

```text
KAP HTML > KAP PDF > TEFAS toplu riskDegeri > TEFAS profil riskDegeri
```

Kurallar:

- Yalnız `riskDegeri`, `RiskDegeri`, `riskValue` alanları okunur.
- Yalnız 1–7 veya n/7 kabul edilir.
- `null`, boş, `-`, 0 ve aralık dışı değerler reddedilir.
- Geçerli KAP riski TEFAS ile ezilmez.
- Profil ve toplu TEFAS değerleri farklıysa çatışma kaydedilir, otomatik risk yazılmaz.
- Her iki TEFAS değeri boşsa risk uydurulmaz.

## İşlem durumu modeli

Birincil nihai TEFAS kaynağı:

```text
fonProfilBilgiGetir.tefasDurum
```

Doğrulama:

```text
getFplFonList
```

Normalizasyon:

```text
AKTİF                     → AÇIK
PASİF                     → KAPALI
TEFAS'ta işlem görüyor    → AÇIK
TEFAS'ta işlem görmüyor   → KAPALI
true / 1                  → AÇIK
false / 0                 → KAPALI
```

Türkçe büyük `İ` için Unicode birleşik işaret temizliği eklendi.

## Canlı test bulguları

- TLY: profil risk 7, toplu risk 7, KAP risk 7.
- BCK: profil `AÇIK`, işlem listesi `EVET/AKTİF`, eski KAP `KAPALI`; nihai `AÇIK`.
- DKC: profil `AÇIK`, işlem listesi `EVET/AKTİF`, eski KAP `KAPALI`; nihai `AÇIK`.
- Test edilen risk-eksik fonlarda TEFAS `riskDegeri` gerçekten `null`; formül hatası yok.

## Checkpoint migrasyonu

- Mevcut 2.138 kayıt korunur.
- Eski satırlar yeni alanları içermese bile yüklenir.
- Eski işlem sonucu `kap_transaction_status` kanıtına taşınır.
- Eski kayıtlar TEFAS profil upgrade kuyruğuna bir kez alınır.
- Teknik profil hataları ayrı sayaçla en fazla 3 kez tekrar edilir.

## Yeni diagnostics

```text
data/diagnostics/tefas_profile_events.jsonl
.run_output/KAP_YAT_SOURCE/TEFAS_PROFIL_JSON/
.run_output/KAP_YAT_SOURCE/TEFAS_TOPLU_RISK/
```

## Workflow

- Varsayılan `max_batches` 40 → 15.
- Neden: fon-bazlı TEFAS profil istekleri arasında 15–20 saniye güvenlik aralığı.
- Profil ve başlangıç POST’ları aynı TEFAS rate limiter sırasını paylaşır.
- Yeni input: `max_tefas_profile_attempts=3`.

## Şema

- Publisher: `github-resumable-v2.6-v9.6`
- Source engine: `v9.6-tefas-profile-risk-trade-1`
- Public/checkpoint schema: `3`
