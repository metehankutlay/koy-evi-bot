# KoyEviBot

emlakjet.com'da satılık müstakil ev / köy evi / köy içi imarlı arsa ilanlarını
İzmir'in tüm ilçeleri + Manisa/Balıkesir/Aydın'ın İzmir'e yakın ilçelerinde tarar,
`config.json`'daki kritere uyan ilanları Telegram'a bildirir. Hem sahibinden hem emlak
ofisi ilanları taranır, her ilanda hangisi olduğu 👤/🏢 etiketiyle belirtilir
(`include_agency_listings: false` yaparsan sadece sahibinden kalır).

Sadece Python standart kütüphanesi kullanır, ekstra paket kurulumu gerekmez.

## 1. Telegram bot kurulumu

1. Telegram'da **@BotFather** ile konuş, `/newbot` yaz, adını belirle.
   Sana bir **token** verecek (örn. `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`).
2. Botunla bir sohbet başlat (Telegram'da botunu bul, `/start` yaz). Bildirimleri
   bir gruba göndermek istersen botu o gruba ekle.
3. Chat ID'ni öğrenmek için tarayıcıda şu adresi aç (token'ı kendi token'ınla değiştir):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Az önce bota attığın `/start` mesajı orada görünecek, içinde `"chat":{"id":123456789,...}` yazan
   sayı senin **chat_id**'n.
   (Gruba eklediysen grup ID'si genelde negatif bir sayıdır, örn. `-1001234567890`.)
4. `config.json` içindeki `telegram.bot_token` ve `telegram.chat_id` alanlarını doldur.

## 2. Test

```bash
cd /Users/mete/KoyEviBot
python3 bot.py --dry-run
```

`--dry-run` Telegram'a mesaj atmaz, bulduğu eşleşmeleri sadece `bot.log`'a ve konsola yazar.
İlk çalıştırmada tüm mevcut eşleşmeler "yeni" sayılır (bir defaya mahsus, veritabanı henüz boş
olduğu için). Bunu istemiyorsan `config.json`'da `"silent_first_run": true` yap — o zaman ilk
çalıştırma sadece veritabanını doldurur, mesaj atmaz; sonraki çalıştırmalarda gerçekten yeni
çıkan ilanlar bildirilir.

Token/chat_id gerçek değerleriyle doldurulduktan sonra `--dry-run` olmadan çalıştırınca
Telegram'a gerçek mesaj gider:

```bash
python3 bot.py
```

## 3. Otomatik çalıştırma (cron)

Her 30 dakikada bir taramak için:

```bash
crontab -e
```

şu satırı ekle:

```
*/30 * * * * cd /Users/mete/KoyEviBot && /usr/bin/python3 bot.py >> cron.log 2>&1
```

Not: Mac uyku moduna geçtiğinde (kapak kapalıyken / ekran kilitliyken) cron çalışmaz.
Bilgisayar sürekli açık/uyanık değilse bunu bir VPS'e (örn. ufak bir DigitalOcean/Hetzner
sunucusu) taşımak daha güvenilir olur — istersen onu da kurarız.

## 4. Kriterleri değiştirmek

Hepsi `config.json` içinde:

- `max_price`: TL cinsinden üst sınır (şu an 1.000.000 — bu bölgede müstakil ev/köy evi
  fiyatları genelde 1.5M TL'nin üstünde, bu yüzden çoğu eşleşme imarlı arsa ve ucuz
  müstakil ev ilanlarından geliyor; bütçeyi yükseltmek istersen söyle)
- `min_size_m2`: minimum m² (şu an `null` = sınır yok)
- `include_agency_listings`: `true` ise hem sahibinden hem emlak ofisi ilanları taranır
  (her ilan 👤 Sahibinden / 🏢 Emlak Ofisinden etiketiyle gösterilir). `false` yaparsan
  sadece sahibinden ilanlar taranır ama tarama süresi yarıya iner (tek istek yeter).
- `locations`: taranacak il-ilçe slug listesi (emlakjet URL formatı, örn. `izmir-urla`,
  `manisa-turgutlu`). Listeye ekleme/çıkarma yapabilirsin — başlangıç listesi tahminimdir,
  gözden geçirmen iyi olur (özellikle Manisa/Balıkesir/Aydın'ın "İzmir'e yakın" ilçeleri
  benim seçimim, senin bölge bilgin daha iyi olabilir).
- `categories`: emlakjet kategori slug'ları. Şu an üçü de açık:
  - `satilik-mustakil-ev` (müstakil ev, bahçeli/avlulu/kargir olanlar `İlan Etiketi`
    alanında veya başlıkta görünüyor, mesajda gösteriliyor)
  - `satilik-koy-evi` (köy evi)
  - `satilik-konut-imarli-arsa` (imarlı arsa — "köy içi" olup olmadığını konum/başlıktan
    kendin değerlendirmen gerekir, bot otomatik ayıklamıyor çünkü emlakjet'te ayrı bir
    "köy içi" filtresi yok)

## 4b. İlçe bazlı Telegram grubu (Forum Topics)

`config.json` → `telegram.forum_chat_id` bir supergroup ID'siyle doldurulursa (Topics
özelliği açık bir grup), bot her ilçe için otomatik ayrı bir "konu" (topic) açar ve o
ilçenin ilanlarını sadece o konuya gönderir. Kurulum: Telegram'da yeni grup aç → grup
ayarlarından "Topics" özelliğini aç (grubu otomatik supergroup'a çevirir) → botu gruba
ekleyip yönetici yap, en azından "Konuları Yönet" (Manage Topics) yetkisini ver → gruba
bir mesaj yaz → `getUpdates` ile grup ID'sini (negatif sayı) bul. `forum_chat_id` boşsa
(`null`) bot eski usul tek `chat_id`'ye mesaj atmaya devam eder.

`python3 bot.py --full-report` mevcut TÜM eşleşen ilanları (seen.db durumuna bakmadan)
ilçe ilçe toplu olarak gönderir — istediğin zaman elle çalıştırıp güncel durumu görebilirsin.

## 4c. Fiyat değişikliği takibi

Her ilanın son bilinen fiyatı `seen.db`'de tutulur. Bir sonraki taramada aynı ilanın
fiyatı değiştiyse (düştü veya arttı) 📉/📈 etiketli ayrı bir bilgi mesajı gönderilir —
o ilanın ilçe topic'ine, eski/yeni fiyat ve fark ile birlikte. Bütçenin üstüne çıkan bir
ilan da (artık normal akışta gösterilmese bile) fiyat değişikliği olarak bildirilir, çünkü
zaten bir kere ilgi çekmiş bir ilandı.

## 4d. Fırsat tespiti ve dashboard

Her ilan için m² fiyatı hesaplanır ve aynı ilçe+kategorideki diğer ilanların **medyan**
m² fiyatıyla karşılaştırılır (ortalama değil medyan kullanılıyor — küçük örneklemde
birkaç pahalı ilan ortalamayı çarpıtıp her şeyi yanıltıcı şekilde ucuz gösterebiliyor).
`deal_discount_threshold` (varsayılan `0.20` = %20) kadar veya daha ucuzsa mesaja
"🔥 Fırsat: m² fiyatı ilçe/kategori medyanından %X ucuz" notu eklenir. En az 3 ilanlık
bir örneklem yoksa fırsat hesaplanmaz (istatistiksel olarak anlamsız olurdu).

Her tarama (`bot.py` normal çalıştırma veya `--full-report`) sonunda proje klasörüne
`dashboard.html` yazılır — çift tıklayıp tarayıcıda açabilirsin. İçinde:
- AI Yatırım Puanına göre en iyi 30 ilan kartı (fiyat, m² fiyatı, ilçe, sahibinden/ofis, linke tıklayınca ilana gider)
- İlçe + kategori bazlı medyan m² fiyat tablosu

## 4e. AI Yatırım Puanı (Beta)

Her ilan için elimizdeki verilerle 100 üzerinden kaba bir puan hesaplanır, mesaja ve
dashboard'a eklenir:

- **Fiyat/m² avantajı — 35 puan:** medyandan ne kadar ucuzsa o kadar puan (%60+ indirimde tavan).
- **Alan büyüklüğü — 20 puan:** 150 m²+ ise tavan puan, altındaysa orantılı azalır.
- **İçinde ev/yapı var mı — 30 puan:** kategori müstakil ev/köy eviyse tam puan; arsa
  ilanının başlığında "ev/villa/yapı/kargir" gibi bir kelime geçiyorsa kısmi puan (22).
- **Elektrik/su bilgisi — 10 puan:** sadece ilan **başlığında** "elektrik/su/altyapı" gibi
  bir ifade geçiyorsa. **Önemli sınırlama:** ilan detay sayfasına gitmiyoruz (istek
  sayısını 2 katına çıkarmamak için), o yüzden bu bilgi çoğu zaman eksik kalır — puanın
  düşük olması "elektrik/su yok" anlamına gelmez, sadece "başlıkta belirtilmemiş" demektir.
  Bu yüzden "Beta" ve kesin bilgi yerine ipucu olarak değerlendirilmeli.
- **Tapu/imar netliği — 5 puan:** İmar Durumu bilgisi varsa veya "parselli"/"müstakil
  tapulu" gibi net bir ifade geçiyorsa.

Bu ağırlıklarla fiyat + alan + ev üçü birlikte tutan bir ilan, elektrik/tapu bilgisi
hiç yakalanamasa bile 70+ puana ulaşabiliyor (örn. medyandan %64 ucuz + 678 m² + yapı
ipucu olan bir ilan → 77/100).

`investment_score` alanı `bot.py` içinde `investment_score()` fonksiyonunda, kriterleri
değiştirmek istersen orayı düzenlemen yeterli.

## 5. Nasıl çalışıyor (teknik not)

- emlakjet'in `robots.txt`'i `min_fiyat=`, `max_fiyat=`, `?filtreler=`, `pageSize=` gibi
  sorgu parametrelerini scraping için yasaklıyor. Bu yüzden bot sadece izin verilen
  path tabanlı URL'leri (`/kategori/il-ilce` ve `/kategori/il-ilce/sahibinden`) çekiyor,
  fiyat filtresini sayfa verisi içinden (JSON-LD) kendisi uyguluyor — robots.txt'ye tam uyumlu.
- Sayfalama `?page=2` ile yapılıyor (yasak listede değil).
- `include_agency_listings: true` olduğunda her kategori/lokasyon için 2 istek atılıyor
  (biri `/sahibinden` ile, biri olmadan), aradaki farktan hangi ilanların sahibinden
  olduğu çıkarılıyor. Bu yüzden tarama süresi yaklaşık 2 katına çıkıyor.
- Her ilan `seen.db` (SQLite) içinde ID'siyle saklanıyor, aynı ilan bir daha bildirilmiyor.
- İstekler arası `request_delay_seconds` (varsayılan 1.5 sn) bekleniyor, siteye yük
  bindirmemek için.

## 6. Facebook grupları (2. aşama, henüz kurulmadı)

Facebook grupları için resmi bir arama API'si yok; taramak için "fake" hesapla giriş yapıp
oturum çerezlerini saklayan bir tarayıcı otomasyonu (Playwright) gerekiyor. Bunu istersen
ayrıca kuracağız — hangi grupların linklerini takip etmek istediğini söylemen yeterli.
Bu kısım emlakjet botundan daha kırılgan olacak (Facebook sayfa yapısını sık değiştiriyor,
otomasyonu tespit etme ihtimali var) ve hesabın engellenme riski var.
