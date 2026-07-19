# Yayın doğrulaması — v2.6 / v9.6

Resmî `data/yat_fund_enrichment.json` yalnızca şu koşullarda değiştirilir:

- KAP YF/Y ana listesi en az 2.000 fon içerir.
- Bütün ana liste kodları staging içinde işlenmiştir.
- Doğrulanmış HTTP 200 + sayfa kodu oranı en az %98'dir.
- Nihai AÇIK/KAPALI işlem durumu oranı en az %98'dir.
- TEFAS profil kontrol oranı en az %98'dir.
- Bekleyen teknik retry, profil upgrade, alan retry veya stale kuyruğu kalmamıştır.

Eksik başlangıç veya risk alanları, teknik hatadan ayrı tutulur. TEFAS ve KAP kaynakları `riskDegeri` yayımlamıyorsa risk alanı boş kalabilir; veri uydurulmaz.

Geçerli KAP riski TEFAS fallback ile ezilmez. TEFAS profil `tefasDurum` ile KAP işlem sonucu çatışırsa nihai TEFAS sonucu kullanılabilir; eski KAP sonucu ve çatışma kanıtı ayrıca saklanır.
