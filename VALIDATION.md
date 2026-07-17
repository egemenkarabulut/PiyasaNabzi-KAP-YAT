# Yayın doğrulaması

Resmî `data/yat_fund_enrichment.json` yalnızca şu koşullarda değiştirilir:

- KAP YF/Y ana listesi en az 2.000 fon içerir.
- Bütün ana liste kodları staging içinde en az bir kez işlenmiştir.
- Doğrulanmış HTTP 200 + sayfa kodu oranı en az %98'dir.
- AÇIK/KAPALI işlem durumu oranı en az %98'dir.

Eksik başlangıç veya risk alanları, geçici HTTP hatasından ayrı tutulur. Alan KAP ve PDF yedeğinde bulunamazsa `SOURCE_NOT_FOUND` olarak yayımlanabilir; veri uydurulmaz.
