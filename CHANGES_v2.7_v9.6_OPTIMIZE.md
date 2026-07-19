# Değişiklikler — Publisher v2.7 / Kaynak Motoru v9.6

## Neden Bu Revizyon Yapıldı?

v2.6, eski checkpoint kayıtlarında yeni TEFAS profil alanları bulunmadığı için güncel aktif KAP evreninin tamamını `tefas_profile_upgrade` kuyruğuna alıyordu. Böylece doğrulanmış 2.000+ kayıt için hem KAP detay sayfası hem tekil profil yeniden çağrılıyordu.

v2.7 bu tam-liste yeniden taramasını kaldırır ve eski KAP çalışma düzenini korur.

## Ana Değişiklikler

### 1. Tam Profil Upgrade Kuyruğu Kaldırıldı

- `NOT_CHECKED` olması tek başına retry nedeni değildir.
- `tefas_profile_upgrade` sayısı her zaman `0` olur.
- Eski doğru KAP kayıtları yalnız yeni profil alanı eksik diye yeniden taranmaz.

### 2. Toplu Hızlı Geçiş Eklendi

Her batch başlangıcında:

1. `fonGetiriBazliBilgiGetir` tek kez alınır.
2. `getFplFonList` tek kez alınır.
3. Mevcut checkpoint kayıtları KAP sayfası açılmadan risk/durum açısından zenginleştirilir.

### 3. Tekil Profil Seçici Hale Getirildi

Tekil `fonProfilBilgiGetir` yalnız şu durumlarda çağrılır:

- toplu `tefasDurum` ile canlı işlem listesi belirsiz veya çelişkiliyse,
- nihai işlem durumu çözülemiyorsa,
- KAP/PDF ve toplu risk kaynaklarının tümü boşsa ve kayıt zaten çalışma kuyruğundaysa.

### 4. KAP Yeniden Tarama Koruması

KAP detay sayfası yalnız eski çalışma kuyruğu için açılır:

- hiç işlenmemiş kayıt,
- teknik KAP hatası,
- TEFAS başlangıç retry,
- eksik alan retry,
- parser upgrade,
- stale kayıt.

Profil retry kaydı için KAP alanları tekrar gerekmiyorsa log:

```text
KAP yeniden tarama YOK
```

### 5. Log Metinleri Düzeltildi

Eski:

```text
Profil REQUEST_ERROR/—
Kategori OK
```

Yeni:

```text
Profil REQUEST_ERROR — Mevcut KAP Değeri Korundu
Kayıt Durumu OK
```

Profil hatasında sahte `KAP↔TEFAS EŞLEŞİYOR` üretilmez. Kesin TEFAS kararı yoksa:

```text
KAP↔TEFAS KONTROL
```

### 6. Bağlantı İstikrarı

- TEFAS toplu ve profil çağrıları aynı `requests.Session` üzerinden yürür.
- Her fonda yeni session açılması kaldırıldı.
- Profil ve başlangıç istekleri eşzamanlı değildir; ortak limiter içinde sırayla çalışır.

### 7. Eski Workflow Ayarları Geri Getirildi

```text
batch_size          = 60
max_batches         = 40
cooldown_seconds    = 180
KAP delay           = 1.35 saniye
TEFAS delay         = 15–20 saniye
```

v2.6'daki `max_batches=15` tekrar eski `40` değerine döndürüldü.

### 8. KAP Liste Farkı Görünür Hale Getirildi

Console ve `run_state.json` artık şunları gösterir:

```text
checkpoint_only_count
checkpoint_only_codes
new_kap_code_count
new_kap_codes
```

Böylece `KAP YF/Y 2134 | Kayıtlı 2138` farkındaki dört kod bir sonraki çalışmada açıkça listelenir.

## Veri Güvenliği

- `data/` dosyaları pakette değiştirilmemiştir.
- Mevcut checkpoint korunur.
- Devam eden workflow iptal edilirse yalnız henüz commit edilmemiş runner değişiklikleri kaybolur.
- Tamamlanıp GitHub'a push edilmiş batch'ler korunur.
