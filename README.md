# Piyasa Nabzı Türkiye — YAT KAP Veri Beslemesi

Bu public GitHub deposu, TEFAS **YAT** fon evrenini alır ve KAP fon detay
sayfalarından aşağıdaki alanları haftalık olarak çıkarır:

- Başlangıç yılı: `Fonun Halka Arz Tarihi`
- Risk bilgisi: `Risk Değeri`
- İşlem durumu: `Alım Satım Yerleri`

## İşlem durumu kuralı

- `AÇIK`: Alım Satım Yerleri alanında **TEFAS** geçiyor.
- `KAPALI`: Alım Satım Yerleri alanı dolu fakat TEFAS geçmiyor.
- `KONTROL`: Alan/bilgi yok veya KAP sayfası doğrulanamadı.

## Dosya yapısı

```text
.github/workflows/update-yat-kap-data.yml
scripts/update_yat_kap_data.py
tests/test_parser.py
data/yat_fund_enrichment.json
client/load_yat_json.py
requirements.txt
```

## Kurulum

1. GitHub'da yeni bir **Public** repository oluşturun.
2. Bu paketin içeriğini repository'nin kök dizinine yükleyin.
3. `Settings > Actions > General > Workflow permissions` bölümünde
   **Read and write permissions** seçeneğini etkinleştirin.
4. `Actions > YAT KAP Verisini Güncelle > Run workflow` yoluyla ilk çalışmayı
   manuel başlatın.
5. İşlem tamamlandığında `data/yat_fund_enrichment.json` dosyasında tüm YAT
   kayıtları oluşur.

## Haftalık çalışma

Workflow her pazar **04:17 Europe/Istanbul** saatinde çalışır. Aynı workflow
manuel olarak da başlatılabilir.

## Programın kullanacağı public adres

```text
https://raw.githubusercontent.com/GITHUB_KULLANICI_ADI/REPO_ADI/main/data/yat_fund_enrichment.json
```

Masaüstü programı yalnızca bu hazır JSON'u indirir. KAP sayfalarını kullanıcı
bilgisayarında tek tek taramaz.

## Güvenli davranış

- TEFAS veya KAP beklenenden çok az fon döndürürse mevcut JSON ezilmez.
- Tek bir fonun KAP isteği başarısız olursa önceki doğrulanmış değeri korunur.
- Önceki değer yoksa işlem durumu `KONTROL` olur.
- JSON geçici dosyaya yazılıp atomik olarak değiştirilir.
- Program tarafı GitHub'a erişemezse yerel cache kullanabilir.

## JSON kaydı örneği

```json
{
  "fund_code": "TLY",
  "start_year": "2021",
  "risk_level": "7",
  "transaction_status": "AÇIK",
  "transaction_reason": "Alım Satım Yerleri alanında TEFAS ifadesi bulundu."
}
```
