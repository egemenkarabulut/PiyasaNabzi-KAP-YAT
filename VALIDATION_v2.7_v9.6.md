# Doğrulama — Publisher v2.7 / Kaynak Motoru v9.6

## Otomatik Testler

```text
pytest: 40 / 40 BAŞARILI
Python compile: BAŞARILI
GitHub Actions YAML parse: BAŞARILI
```

## Veri Koruma Kontrolü

v2.6 ile v2.7 `data/` klasörleri birebir karşılaştırıldı:

```text
FARK YOK
```

Ana dosya SHA-256 değerleri:

```text
run_state.json
  ae3b4aa242c0b7f4b4fe35661238aeaf0a5943c97e5bb403f4ef3b27fae0868c

yat_kap_progress.json
  24af180bdf6de3dda4d9f9d64d4a83a97c0ea4680fec569fc27adb249921c950

yat_fund_enrichment.json
  7680d1625274ff054c22be521c71c6154fbb89b71393d81338fec8c97c7f7d84
```

## Kuyruk Regresyon Kontrolü

2.138 kayıtlı checkpoint ve arşivlenmiş 2.138 satırlı TEFAS toplu snapshot üzerinde yapılan çevrimdışı kontrolde:

```text
tefas_profile_upgrade : 0
pending_total         : 112
parser upgrade        : 97
seçici profile retry  : 15
```

Bu kontrol, eski bütün kayıtların tekrar 2.134'lük profil kuyruğuna alınmadığını doğrular.

## Kritik Kural Testleri

- Eski tamamlanmış `NOT_CHECKED` kayıt yalnız profil alanı yok diye kuyruğa alınmaz.
- Toplu `AÇIK` + işlem listesi `EVET` profil çağrısı olmadan `AÇIK` üretir.
- Toplu `KAPALI` + işlem listesi `HAYIR` profil çağrısı olmadan `KAPALI` üretir.
- Profil `REQUEST_ERROR` olduğunda mevcut KAP sonucu korunur.
- Profil hatasında `KAP↔TEFAS` değeri `KONTROL` kalır.
- TLY risk 7 formülü korunur.
- BCK null risk için değer uydurulmaz.
- BCK profil doğrulaması mevcutsa KAP çatışması korunarak `AÇIK` kabul edilir.
- Türkçe `AKTİF/PASİF` Unicode normalizasyonu çalışır.

## Workflow Ayar Kontrolü

```text
batch_size                  = 60
max_batches                 = 40
cooldown_seconds            = 180
delay_seconds               = 1.35
tefas_start_delay_min       = 15
tefas_start_delay_max       = 20
max_tefas_profile_attempts  = 3
```
