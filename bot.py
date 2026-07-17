#!/usr/bin/env python3
"""
KoyEviBot - emlakjet.com'da sahibinden köy evi / müstakil ev / imarlı arsa
ilanlarını tarar, config.json'daki kritere uyan YENİ ilanları Telegram'a bildirir.

Kullanım:
    python3 bot.py            # normal çalıştırma (cron için)
    python3 bot.py --dry-run  # Telegram'a mesaj atmadan sadece konsola yazdır
"""
import json
import os
import re
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "seen.db"
LOG_PATH = BASE_DIR / "bot.log"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

LDJSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S
)
ID_RE = re.compile(r"-(\d+)/?$")

CATEGORY_LABELS = {
    "satilik-mustakil-ev": "Müstakil Ev",
    "satilik-koy-evi": "Köy Evi",
    "satilik-konut-imarli-arsa": "İmarlı Arsa",
}

PROVINCE_LABELS = {
    "izmir": "İzmir",
    "manisa": "Manisa",
    "balikesir": "Balıkesir",
    "aydin": "Aydın",
}


def province_of(listing):
    slug = listing.get("location_slug", "")
    province_slug = slug.split("-", 1)[0]
    return PROVINCE_LABELS.get(province_slug, province_slug.capitalize())

DASHBOARD_PATH = BASE_DIR / "dashboard.html"


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    """config.json (repoda takip edilen, sır içermeyen ayarlar) üzerine, varsa
    secrets.local.json (yerelde .gitignore'lu, gerçek Telegram bilgileri) ve varsa
    ortam değişkenleri (GitHub Actions secrets) sırasıyla bindirilir."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    secrets_path = BASE_DIR / "secrets.local.json"
    if secrets_path.exists():
        with open(secrets_path, "r", encoding="utf-8") as f:
            config.setdefault("telegram", {}).update(json.load(f))

    telegram = config.setdefault("telegram", {})
    for key, env_var in [
        ("bot_token", "TELEGRAM_BOT_TOKEN"),
        ("chat_id", "TELEGRAM_CHAT_ID"),
        ("forum_chat_id", "TELEGRAM_FORUM_CHAT_ID"),
    ]:
        if os.environ.get(env_var):
            telegram[key] = os.environ[env_var]

    return config


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS seen (
            id TEXT PRIMARY KEY,
            category TEXT,
            location TEXT,
            first_seen TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS topics (
            district TEXT PRIMARY KEY,
            thread_id INTEGER
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS geocode_cache (
            query TEXT PRIMARY KEY,
            lat REAL,
            lon REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS price_history (
            listing_id TEXT,
            price INTEGER,
            observed_at TEXT
        )"""
    )
    try:
        conn.execute("ALTER TABLE seen ADD COLUMN last_price INTEGER")
    except sqlite3.OperationalError:
        pass  # kolon zaten var
    try:
        conn.execute("ALTER TABLE seen ADD COLUMN last_notified TEXT")
    except sqlite3.OperationalError:
        pass  # kolon zaten var
    conn.commit()
    return conn


def log_price_history(conn, listing_id, price):
    if price is None:
        return
    row = conn.execute(
        "SELECT price FROM price_history WHERE listing_id = ? ORDER BY observed_at DESC LIMIT 1",
        (listing_id,),
    ).fetchone()
    if row and row[0] == price:
        return  # fiyat değişmediyse tekrar tekrar aynı satırı eklemeye gerek yok
    conn.execute(
        "INSERT INTO price_history (listing_id, price, observed_at) VALUES (?, ?, ?)",
        (listing_id, price, time.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


GEOCODE_USER_AGENT = "KoyEviBot/1.0 (kişisel emlak takip botu; github.com/metehankutlay/koy-evi-bot)"


def geocode(conn, query):
    """OpenStreetMap Nominatim ile bir adres metnini (lat, lon) çevirir, sonucu
    geocode_cache'te saklar. Nominatim kullanım politikası saniyede 1 istekle
    sınırlı olduğu için sadece önbellekte OLMAYAN sorgular için gerçek istek atılır,
    her yeni istekten sonra 1.1sn beklenir."""
    row = conn.execute(
        "SELECT lat, lon FROM geocode_cache WHERE query = ?", (query,)
    ).fetchone()
    if row:
        return row[0], row[1]

    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1, "countrycodes": "tr"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": GEOCODE_USER_AGENT})
    lat, lon = None, None
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        if data:
            lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        log(f"Geocode hatası ({query}): {e}")

    conn.execute(
        "INSERT OR REPLACE INTO geocode_cache (query, lat, lon) VALUES (?, ?, ?)",
        (query, lat, lon),
    )
    conn.commit()
    time.sleep(1.1)  # Nominatim kullanım politikası: en fazla 1 istek/saniye
    return lat, lon


def parse_listings(html):
    """emlakjet sayfasındaki JSON-LD @graph bloğundan ilan listesini çıkarır."""
    listings = []
    for block in LDJSON_RE.findall(html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        items = data.get("@graph") if isinstance(data, dict) else None
        if not items:
            continue
        for item in items:
            if item.get("@type") != "RealEstateListing":
                continue
            listings.append(item)
    return listings


def normalize(item):
    url = item.get("url", "")
    m = ID_RE.search(url)
    listing_id = m.group(1) if m else url
    offers = item.get("offers") or {}
    price = offers.get("price")
    props = {
        p.get("name"): p.get("value")
        for p in item.get("additionalProperty", [])
        if isinstance(p, dict)
    }
    return {
        "id": listing_id,
        "name": item.get("name", "").strip(),
        "url": url,
        "price": price,
        "location": props.get("Konum", ""),
        "m2": props.get("Metrekare", ""),
        "rooms": props.get("Oda Sayısı", ""),
        "tag": props.get("İlan Etiketi", ""),
        "imar": props.get("İmar Durumu", ""),
        "date_posted": item.get("datePosted", ""),
    }


def query_matches(config, category, location, sahibinden_only=True):
    """Bir kategori+lokasyon için tüm sayfaları gezip filtreye uyan ilanları döner."""
    results = []
    max_pages = config.get("max_pages_per_query", 5)
    delay = config.get("request_delay_seconds", 1.5)
    base_url = f"https://www.emlakjet.com/{category}/{location}"
    if sahibinden_only:
        base_url += "/sahibinden"

    for page in range(1, max_pages + 1):
        url = base_url if page == 1 else f"{base_url}?page={page}"
        try:
            html = fetch(url)
        except Exception as e:
            log(f"HATA: {url} -> {e}")
            break

        raw_items = parse_listings(html)
        if not raw_items:
            break

        for raw in raw_items:
            listing = normalize(raw)
            listing["category"] = category
            listing["location_slug"] = location
            results.append(listing)

        # Bu kategori/lokasyon için sayfa sonu genelde 30'dan az döner; daha az geldiyse son sayfadayız demektir.
        if len(raw_items) < 30:
            break

        time.sleep(delay)

    return results


def parse_m2_value(listing):
    m2_text = (listing.get("m2") or "").replace("m²", "").replace(".", "").strip()
    digits = re.sub(r"[^\d]", "", m2_text)
    return int(digits) if digits else None


def passes_filters(listing, config):
    max_price = config.get("max_price")
    min_size = config.get("min_size_m2")
    min_price = config.get("min_price", 50000)

    if listing["price"] is None:
        return False
    if max_price and listing["price"] > max_price:
        return False
    if min_price and listing["price"] < min_price:
        return False  # gerçekçi olmayan/hatalı fiyat girişleri (ör. 12.500 TL'lik "arsa")

    if min_size:
        m2 = parse_m2_value(listing)
        if m2 and m2 < min_size:
            return False

    return True


def price_per_m2(listing):
    m2 = parse_m2_value(listing)
    price = listing.get("price")
    if not m2 or not price:
        return None
    return price / m2


def attach_deal_info(listings, threshold=0.20, min_sample=3):
    """Bir (ilçe, kategori) grubundaki ilanlara m² fiyatını ve grubun tipik (medyan) m²
    fiyatına göre 'fırsat' notunu ekler. Medyan kullanılıyor çünkü bu niş piyasada
    örneklem küçük ve birkaç pahalı ilan ortalamayı yukarı çekip her şeyi yanıltıcı
    şekilde 'ucuz' gösterebiliyor. Örneklem min_sample'dan küçükse fırsat hesaplanmaz."""
    ppm_values = []
    for l in listings:
        ppm = price_per_m2(l)
        l["price_per_m2"] = ppm
        if ppm:
            ppm_values.append(ppm)

    typical = statistics.median(ppm_values) if len(ppm_values) >= min_sample else None

    for l in listings:
        l["district_avg_price_per_m2"] = typical
        ppm = l["price_per_m2"]
        if ppm and typical and ppm < typical * (1 - threshold):
            discount_pct = round((1 - ppm / typical) * 100)
            l["deal_note"] = f"🔥 Fırsat: m² fiyatı ilçe/kategori medyanından %{discount_pct} ucuz"
        else:
            l["deal_note"] = None

        l["investment_score"], l["investment_score_notes"] = investment_score(l)

    return listings


HOUSE_KEYWORDS = ["ev", "villa", "bina", "yapı", "kargir", "müstakil", "dubleks"]
UTILITY_KEYWORDS = [
    "elektrik", "elektirik", "elektrikli", "elektırık",
    "sulu", "su var", "içme suyu", "altyapı",
]


MIN_ADVANTAGED_SIZE_M2 = 150


def investment_score(listing):
    """Elimizdeki verilerle (başlık, kategori, fiyat/m² avantajı, alan büyüklüğü, imar
    bilgisi) kaba bir 100 üzerinden yatırım puanı üretir. İlan detay sayfasına gitmediğimiz
    için elektrik/su gibi bilgiler sadece BAŞLIKTA geçiyorsa yakalanabiliyor — bu yüzden
    'Beta': kesin bilgi değil, başlık metninden çıkarım. Skor + skora katkı veren notları
    döner.

    Ağırlıklar: fiyat/m² avantajı 35, alan büyüklüğü 20, ev/yapı varlığı 30,
    elektrik/su ipucu 10, tapu/imar netliği 5 (toplam 100). Fiyat + alan + ev üçü
    birlikte tutarsa elektrik/tapu bilgisi olmasa bile 70+ puana ulaşılabiliyor."""
    title_lower = (listing.get("name") or "").lower()
    tag_lower = (listing.get("tag") or "").lower()
    score = 0
    notes = []

    # 1) Fiyat/m² avantajı - 35 puan (medyandan %60+ ucuzsa tavan puan)
    ppm = listing.get("price_per_m2")
    median = listing.get("district_avg_price_per_m2")
    if ppm and median and ppm < median:
        discount_pct = (1 - ppm / median) * 100
        score += min(discount_pct, 60) / 60 * 35
        if discount_pct >= 20:
            notes.append(f"💰 Fiyat/m² avantajlı (medyandan %{discount_pct:.0f} ucuz)")

    # 2) Alan büyüklüğü - 20 puan (150 m²+ tavan puan, altında orantılı)
    m2 = parse_m2_value(listing)
    if m2:
        size_score = min(m2 / MIN_ADVANTAGED_SIZE_M2, 1) * 20
        score += size_score
        if m2 >= MIN_ADVANTAGED_SIZE_M2:
            notes.append(f"📐 Geniş alan ({m2} m², {MIN_ADVANTAGED_SIZE_M2}+ m²)")

    # 3) İçinde ev/yapı var mı - 30 puan
    if listing.get("category") in ("satilik-mustakil-ev", "satilik-koy-evi"):
        score += 30
        notes.append("🏠 İçinde ev var")
    elif any(k in title_lower for k in HOUSE_KEYWORDS):
        score += 22
        notes.append("🏠 Üzerinde yapı olabilir (başlıkta belirtilmiş)")

    # 4) Elektrik/su bilgisi (sadece başlıktan çıkarım) - 10 puan
    if any(k in title_lower for k in UTILITY_KEYWORDS):
        score += 10
        notes.append("⚡💧 Elektrik/su bilgisi başlıkta geçiyor")

    # 5) Tapu/imar netliği - 5 puan
    if listing.get("imar") or "parselli" in tag_lower or "müstakil tapulu" in title_lower:
        score += 5
        notes.append("🧾 Tapu/imar durumu net")

    return round(min(score, 100)), notes


def owner_label(listing):
    return "👤 Sahibinden" if listing.get("is_sahibinden") else "🏢 Emlak Ofisinden"


def _common_detail_lines(listing):
    """format_message ve format_price_change_message'ın paylaştığı satırlar:
    konum, m², ilan tarihi, m² fiyatı/fırsat notu."""
    lines = []
    if listing.get("location"):
        lines.append(f"📍 {listing['location']}, {province_of(listing)}")
    if listing.get("m2"):
        lines.append(f"📐 {listing['m2']}")
    if listing.get("date_posted"):
        lines.append(f"🗓 İlan Tarihi: {listing['date_posted']}")
    if listing.get("price_per_m2"):
        ppm_str = f"{listing['price_per_m2']:,.0f} TL/m²".replace(",", ".")
        line = f"📊 {ppm_str}"
        if listing.get("district_avg_price_per_m2"):
            avg_str = f"{listing['district_avg_price_per_m2']:,.0f} TL/m²".replace(",", ".")
            line += f" (ilçe/kategori medyanı: {avg_str})"
        lines.append(line)
    if listing.get("deal_note"):
        lines.append(listing["deal_note"])
    if listing.get("investment_score") is not None:
        lines.append(f"🤖 AI Yatırım Puanı (Beta): {listing['investment_score']}/100")
        for note in listing.get("investment_score_notes", []):
            lines.append(f"   • {note}")
    return lines


def format_message(listing):
    price = listing["price"]
    price_str = f"{price:,.0f} TL".replace(",", ".") if price else "Fiyat belirtilmemiş"
    category_label = CATEGORY_LABELS.get(listing["category"], listing["category"])

    lines = [
        owner_label(listing),
        f"🏡 <b>{listing['name']}</b>",
        f"🏷 {category_label}" + (f" ({listing['tag']})" if listing.get("tag") else ""),
        f"💰 {price_str}",
    ]
    lines += _common_detail_lines(listing)
    if listing.get("imar"):
        lines.append(f"🧾 İmar: {listing['imar']}")
    lines.append(listing["url"])
    return "\n".join(lines)


def format_price_change_message(listing, old_price, new_price):
    direction = "📉 Fiyat Düştü" if new_price < old_price else "📈 Fiyat Arttı"
    old_str = f"{old_price:,.0f} TL".replace(",", ".")
    new_str = f"{new_price:,.0f} TL".replace(",", ".")
    diff = abs(new_price - old_price)
    diff_str = f"{diff:,.0f} TL".replace(",", ".")
    lines = [
        f"{direction}",
        f"🏡 <b>{listing['name']}</b>",
        f"💰 {old_str} → {new_str} ({'-' if new_price < old_price else '+'}{diff_str})",
    ]
    lines += _common_detail_lines(listing)
    lines.append(listing["url"])
    return "\n".join(lines)


def telegram_api(config, method, payload):
    token = config["telegram"]["bot_token"]
    api_url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        api_url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def send_telegram(config, text, dry_run=False, chat_id=None, thread_id=None):
    token = config["telegram"]["bot_token"]
    chat_id = chat_id or config["telegram"]["chat_id"]
    if dry_run or "PUT_YOUR" in token or "PUT_YOUR" in str(chat_id):
        log("DRY-RUN / Telegram yapılandırılmamış, mesaj konsola yazılıyor:\n" + text)
        return

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if thread_id:
        payload["message_thread_id"] = thread_id

    try:
        telegram_api(config, "sendMessage", payload)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            try:
                retry_after = json.loads(e.read())["parameters"]["retry_after"]
            except Exception:
                retry_after = 5
            log(f"Telegram 429 (rate limit), {retry_after}sn bekleyip tekrar deneniyor.")
            time.sleep(retry_after + 1)
            try:
                telegram_api(config, "sendMessage", payload)
            except Exception as e2:
                log(f"Telegram gönderim hatası (retry sonrası): {e2}")
        else:
            log(f"Telegram gönderim hatası: {e}")
    except Exception as e:
        log(f"Telegram gönderim hatası: {e}")


def forum_configured(config):
    forum_chat_id = config.get("telegram", {}).get("forum_chat_id")
    return bool(forum_chat_id) and "PUT_YOUR" not in str(forum_chat_id)


def get_topic_thread_id(conn, config, district, dry_run=False):
    """Bir ilçe için forum-topic thread_id döner; yoksa oluşturur ve DB'ye kaydeder."""
    row = conn.execute(
        "SELECT thread_id FROM topics WHERE district = ?", (district,)
    ).fetchone()
    if row:
        return row[0]

    forum_chat_id = config["telegram"]["forum_chat_id"]
    if dry_run:
        log(f"DRY-RUN: '{district}' için yeni topic oluşturulacaktı.")
        return None

    payload = {"chat_id": forum_chat_id, "name": district}
    for attempt in range(2):
        try:
            result = telegram_api(config, "createForumTopic", payload)
            thread_id = result["result"]["message_thread_id"]
            conn.execute(
                "INSERT INTO topics (district, thread_id) VALUES (?, ?)",
                (district, thread_id),
            )
            conn.commit()
            return thread_id
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                try:
                    retry_after = json.loads(e.read())["parameters"]["retry_after"]
                except Exception:
                    retry_after = 5
                log(f"Topic oluşturma 429 ({district}), {retry_after}sn bekleyip tekrar deneniyor.")
                time.sleep(retry_after + 1)
                continue
            log(f"Topic oluşturma hatası ({district}): {e}")
            return None
        except Exception as e:
            log(f"Topic oluşturma hatası ({district}): {e}")
            return None


def fetch_category_listings(config, category, location):
    """Bir kategori+lokasyon için ilanları çeker. include_agency_listings açıksa hem
    sahibinden hem emlak ofisi ilanlarını getirip her birini işaretler; kapalıysa
    sadece sahibinden ilanlarını döner."""
    delay = config.get("request_delay_seconds", 1.5)
    include_agency = config.get("include_agency_listings", True)

    if not include_agency:
        listings = query_matches(config, category, location, sahibinden_only=True)
        for l in listings:
            l["is_sahibinden"] = True
        attach_deal_info(listings, threshold=config.get("deal_discount_threshold", 0.20))
        return listings

    sahibinden_listings = query_matches(config, category, location, sahibinden_only=True)
    sahibinden_ids = {l["id"] for l in sahibinden_listings}
    time.sleep(delay)
    all_listings = query_matches(config, category, location, sahibinden_only=False)
    for l in all_listings:
        l["is_sahibinden"] = l["id"] in sahibinden_ids

    attach_deal_info(all_listings, threshold=config.get("deal_discount_threshold", 0.20))
    return all_listings


def collect_all_matches(config, log_progress=True):
    """Tüm lokasyon/kategori kombinasyonlarını tarar, filtreye uyan ilanları döner
    (seen.db durumuna bakmadan - o an sitede olan tüm eşleşmeler). Aynı ilan birden
    fazla kategori sorgusunda çıkarsa (emlakjet bazen aynı ilanı birden fazla kategoride
    listeler) sadece bir kez sayılır."""
    all_matches = []
    seen_ids = set()
    for location in config["locations"]:
        for category in config["categories"]:
            try:
                matches = fetch_category_listings(config, category, location)
            except Exception as e:
                log(f"HATA: {category}/{location} -> {e}")
                continue

            for listing in matches:
                if listing["id"] in seen_ids:
                    continue
                if passes_filters(listing, config):
                    seen_ids.add(listing["id"])
                    all_matches.append(listing)

            time.sleep(config.get("request_delay_seconds", 1.5))

    if log_progress:
        log(f"Tarama bitti. {len(all_matches)} eşleşme bulundu.")
    return all_matches


def _html_escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def generate_dashboard(conn, matches):
    """matches listesinden etkileşimli bir dashboard.html üretir: mahalle bazlı harita
    (OSM Nominatim ile geocode edilmiş), sıralanabilir/filtrelenebilir ilan listesi,
    ilçe+kategori bazlı m² dağılımı ve fiyat geçmişi. Tüm etkileşim istemci tarafında
    (gömülü JSON + vanilla JS) çalışır, sunucu/backend gerekmez."""

    # 1) Mahalle bazlı geocoding + istatistik (harita için)
    mahalle_groups = {}
    for l in matches:
        loc = l.get("location") or district_of(l)
        mahalle_groups.setdefault(loc, []).append(l)

    mahalle_points = []
    for loc, listings in mahalle_groups.items():
        query = f"{loc}, {province_of(listings[0])}, Türkiye"
        lat, lon = geocode(conn, query)
        if lat is None:
            continue
        ppm_values = [l["price_per_m2"] for l in listings if l.get("price_per_m2")]
        mahalle_points.append(
            {
                "name": loc,
                "district": district_of(listings[0]),
                "lat": lat,
                "lon": lon,
                "count": len(listings),
                "medianPpm": round(statistics.median(ppm_values)) if ppm_values else None,
            }
        )

    # 2) İlçe+kategori bazlı m² dağılımı (nadir büyüklük tespiti + histogram için)
    district_groups = {}
    for l in matches:
        key = (district_of(l), l["category"])
        district_groups.setdefault(key, []).append(l)

    district_stats = {}
    for (district, category), listings in district_groups.items():
        m2_values = sorted(v for v in (parse_m2_value(l) for l in listings) if v)
        ppm_values = [l["price_per_m2"] for l in listings if l.get("price_per_m2")]
        district_stats[f"{district}|{category}"] = {
            "district": district,
            "category": category,
            "categoryLabel": CATEGORY_LABELS.get(category, category),
            "count": len(listings),
            "medianPpm": round(statistics.median(ppm_values)) if ppm_values else None,
            "medianM2": round(statistics.median(m2_values)) if m2_values else None,
            "m2Values": m2_values,
        }

    # 3) Her ilan için detay + fiyat geçmişi
    listing_data = []
    for l in matches:
        history = conn.execute(
            "SELECT price, observed_at FROM price_history WHERE listing_id = ? ORDER BY observed_at",
            (l["id"],),
        ).fetchall()
        stat_key = f"{district_of(l)}|{l['category']}"
        med_m2 = district_stats.get(stat_key, {}).get("medianM2")
        m2_val = parse_m2_value(l)
        rare_size = bool(
            med_m2 and m2_val and (m2_val > med_m2 * 2 or m2_val < med_m2 * 0.5)
        )
        listing_data.append(
            {
                "id": l["id"],
                "name": l["name"],
                "url": l["url"],
                "price": l["price"],
                "m2": m2_val,
                "ppm": round(l["price_per_m2"]) if l.get("price_per_m2") else None,
                "category": l["category"],
                "categoryLabel": CATEGORY_LABELS.get(l["category"], l["category"]),
                "district": district_of(l),
                "province": province_of(l),
                "location": l.get("location", ""),
                "isSahibinden": bool(l.get("is_sahibinden")),
                "score": l.get("investment_score", 0),
                "scoreNotes": l.get("investment_score_notes", []),
                "dealNote": l.get("deal_note"),
                "datePosted": l.get("date_posted"),
                "rareSize": rare_size,
                "history": [{"price": p, "date": d} for p, d in history],
            }
        )

    deal_count = sum(1 for l in matches if l.get("deal_note"))
    top_score = max((l.get("investment_score", 0) for l in matches), default=0)
    districts = sorted({l["district"] for l in listing_data})

    listings_json = json.dumps(listing_data, ensure_ascii=False)
    mahalle_json = json.dumps(mahalle_points, ensure_ascii=False)
    district_stats_json = json.dumps(list(district_stats.values()), ensure_ascii=False)
    districts_json = json.dumps(districts, ensure_ascii=False)
    category_labels_json = json.dumps(CATEGORY_LABELS, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<meta charset="UTF-8">
<title>KöyEviBot Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root {{ color-scheme: light dark; --accent: #f60; --deal: #d9480f; --score: #7048e8; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 1100px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.4rem; }}
  h2 {{ font-size: 1.1rem; margin-top: 2rem; border-bottom: 1px solid #8884; padding-bottom: .3rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
  th, td {{ text-align: left; padding: .5rem .6rem; border-bottom: 1px solid #8883; vertical-align: top; }}
  th {{ cursor: pointer; user-select: none; white-space: nowrap; }}
  th:hover {{ color: var(--accent); }}
  tr.district-row {{ cursor: pointer; }}
  tr.district-row:hover {{ background: #8881; }}
  tr.listing-row {{ cursor: pointer; }}
  tr.listing-row:hover {{ background: #8881; }}
  tr.detail-row td {{ background: #8880a; padding: 1rem; }}
  .updated {{ opacity: .6; font-size: .85rem; }}
  .badge {{ display: inline-block; padding: .15rem .55rem; border-radius: 1rem; font-size: .75rem; font-weight: 600; white-space: nowrap; }}
  .badge-owner {{ background: #2f9e44; color: #fff; }}
  .badge-agency {{ background: #1971c2; color: #fff; }}
  .badge-score {{ background: var(--score); color: #fff; }}
  .badge-deal {{ background: var(--deal); color: #fff; }}
  .badge-rare {{ background: #e8590c; color: #fff; }}
  #map {{ height: 420px; border-radius: .6rem; margin-top: .6rem; }}
  .filters {{ display: flex; flex-wrap: wrap; gap: .5rem; margin: .8rem 0; align-items: center; }}
  .filters input, .filters select {{ padding: .4rem .6rem; border-radius: .4rem; border: 1px solid #8886; background: transparent; color: inherit; }}
  .filters input[type=text] {{ flex: 1; min-width: 180px; }}
  .stat-row {{ display: flex; gap: 1.5rem; flex-wrap: wrap; margin: .5rem 0 1rem; font-size: .9rem; opacity: .85; }}
  .hist {{ display: flex; align-items: flex-end; gap: 2px; height: 60px; margin-top: .4rem; }}
  .hist div {{ background: var(--accent); flex: 1; min-width: 4px; border-radius: 2px 2px 0 0; opacity: .8; }}
  .link {{ color: var(--accent); text-decoration: none; }}
  .link:hover {{ text-decoration: underline; }}
  .name-cell {{ max-width: 320px; }}
  .clear-btn {{ background: transparent; border: 1px solid #8886; border-radius: .4rem; padding: .4rem .7rem; cursor: pointer; color: inherit; }}
</style>

<h1>🏡 KöyEviBot Dashboard</h1>
<p class="updated">Son güncelleme: {time.strftime('%Y-%m-%d %H:%M:%S')}</p>
<div class="stat-row">
  <span>📋 {len(matches)} ilan</span>
  <span>🔥 {deal_count} fırsat</span>
  <span>🤖 En yüksek puan: {top_score}/100</span>
  <span>📍 {len(mahalle_points)} mahalle haritada</span>
</div>

<h2>🗺️ Mahalle Bazlı m² Fiyat Haritası</h2>
<div id="map"></div>

<h2>📋 İlan Listesi</h2>
<div class="filters">
  <input type="text" id="f-search" placeholder="Başlıkta ara...">
  <select id="f-district"><option value="">Tüm ilçeler</option></select>
  <select id="f-category"><option value="">Tüm kategoriler</option></select>
  <select id="f-owner">
    <option value="">Sahibinden/Ofis (hepsi)</option>
    <option value="1">👤 Sadece Sahibinden</option>
    <option value="0">🏢 Sadece Emlak Ofisi</option>
  </select>
  <select id="f-sort">
    <option value="score">🤖 AI Puanına göre sırala</option>
    <option value="price">💰 Fiyata göre (artan)</option>
    <option value="ppm">📊 m² fiyatına göre (artan)</option>
    <option value="m2">📐 Alana göre (azalan)</option>
  </select>
  <button class="clear-btn" id="f-clear">Filtreleri Temizle</button>
</div>
<p id="result-count" style="opacity:.7; font-size:.85rem;"></p>
<table>
  <thead>
    <tr>
      <th>🤖</th><th>İlan</th><th>İlçe</th><th>Kategori</th><th>Tip</th>
      <th>Fiyat</th><th>m²</th><th>m² Fiyatı</th>
    </tr>
  </thead>
  <tbody id="listing-body"></tbody>
</table>

<h2>📊 İlçe / Kategori Bazlı Medyan m² Fiyatları ve Alan Dağılımı</h2>
<p style="opacity:.7; font-size:.85rem;">Bir satıra tıklayınca o ilçe/kategorideki tüm ilanlar yukarıdaki listede filtrelenir.</p>
<table>
  <thead><tr><th>İlçe</th><th>Kategori</th><th>İlan Sayısı</th><th>Medyan m² Fiyatı</th><th>Medyan Alan</th><th>Alan Dağılımı</th></tr></thead>
  <tbody id="district-body"></tbody>
</table>

<script>
const LISTINGS = {listings_json};
const MAHALLE_POINTS = {mahalle_json};
const DISTRICT_STATS = {district_stats_json};
const DISTRICTS = {districts_json};
const CATEGORY_LABELS = {category_labels_json};

function tl(n) {{
  if (n === null || n === undefined) return '—';
  return Math.round(n).toLocaleString('tr-TR') + ' TL';
}}
function ppmStr(n) {{
  if (n === null || n === undefined) return '—';
  return Math.round(n).toLocaleString('tr-TR') + ' TL/m²';
}}

// --- Filtre kontrollerini doldur ---
const districtSelect = document.getElementById('f-district');
DISTRICTS.forEach(d => {{
  const opt = document.createElement('option');
  opt.value = d; opt.textContent = d;
  districtSelect.appendChild(opt);
}});
const categorySelect = document.getElementById('f-category');
Object.entries(CATEGORY_LABELS).forEach(([key, label]) => {{
  const opt = document.createElement('option');
  opt.value = key; opt.textContent = label;
  categorySelect.appendChild(opt);
}});

const state = {{ search: '', district: '', category: '', owner: '', sort: 'score', expanded: null }};

function applyFilters() {{
  let rows = LISTINGS.filter(l => {{
    if (state.search && !l.name.toLowerCase().includes(state.search.toLowerCase())) return false;
    if (state.district && l.district !== state.district) return false;
    if (state.category && l.category !== state.category) return false;
    if (state.owner === '1' && !l.isSahibinden) return false;
    if (state.owner === '0' && l.isSahibinden) return false;
    return true;
  }});
  rows.sort((a, b) => {{
    if (state.sort === 'score') return (b.score||0) - (a.score||0);
    if (state.sort === 'price') return (a.price||Infinity) - (b.price||Infinity);
    if (state.sort === 'ppm') return (a.ppm||Infinity) - (b.ppm||Infinity);
    if (state.sort === 'm2') return (b.m2||0) - (a.m2||0);
    return 0;
  }});
  return rows;
}}

function historySparkline(history) {{
  if (!history || history.length < 2) return '<p style="opacity:.6">Fiyat geçmişi henüz tek nokta, değişiklik yok.</p>';
  const items = history.map(h => `${{h.date.split(' ')[0]}}: ${{tl(h.price)}}`).join(' → ');
  return `<p><b>Fiyat geçmişi:</b> ${{items}}</p>`;
}}

function scoreNotesHtml(notes) {{
  if (!notes || !notes.length) return '';
  return '<ul style="margin:.3rem 0 0 1.1rem; padding:0;">' + notes.map(n => `<li>${{n}}</li>`).join('') + '</ul>';
}}

function renderList() {{
  const rows = applyFilters();
  document.getElementById('result-count').textContent = `${{rows.length}} ilan gösteriliyor (toplam ${{LISTINGS.length}})`;
  const body = document.getElementById('listing-body');
  body.innerHTML = '';
  rows.forEach(l => {{
    const tr = document.createElement('tr');
    tr.className = 'listing-row';
    const ownerBadge = l.isSahibinden
      ? '<span class="badge badge-owner">👤 Sahibinden</span>'
      : '<span class="badge badge-agency">🏢 Ofis</span>';
    tr.innerHTML = `
      <td><span class="badge badge-score">${{l.score}}</span></td>
      <td class="name-cell">${{l.name}}${{l.dealNote ? '<br><span class="badge badge-deal" style="margin-top:.2rem">🔥 Fırsat</span>' : ''}}${{l.rareSize ? ' <span class="badge badge-rare">📏 Nadir m²</span>' : ''}}</td>
      <td>${{l.district}}</td>
      <td>${{l.categoryLabel}}</td>
      <td>${{ownerBadge}}</td>
      <td>${{tl(l.price)}}</td>
      <td>${{l.m2 || '—'}}</td>
      <td>${{ppmStr(l.ppm)}}</td>
    `;
    tr.addEventListener('click', () => {{
      state.expanded = state.expanded === l.id ? null : l.id;
      renderList();
    }});
    body.appendChild(tr);

    if (state.expanded === l.id) {{
      const detailTr = document.createElement('tr');
      detailTr.className = 'detail-row';
      detailTr.innerHTML = `<td colspan="8">
        <p><a class="link" href="${{l.url}}" target="_blank" rel="noopener">İlana git ↗</a> · ${{l.location}}, ${{l.province}} · İlan tarihi: ${{l.datePosted || '—'}}</p>
        ${{l.dealNote ? `<p>🔥 ${{l.dealNote}}</p>` : ''}}
        <p><b>AI Yatırım Puanı: ${{l.score}}/100</b>${{scoreNotesHtml(l.scoreNotes)}}</p>
        ${{historySparkline(l.history)}}
      </td>`;
      body.appendChild(detailTr);
    }}
  }});
}}

function histogramSvg(values) {{
  if (!values || values.length < 3) return '<span style="opacity:.5">yetersiz veri</span>';
  const min = values[0], max = values[values.length - 1];
  if (min === max) return '<span style="opacity:.5">tek değer</span>';
  const bins = 8;
  const width = max - min;
  const counts = new Array(bins).fill(0);
  values.forEach(v => {{
    let idx = Math.floor(((v - min) / width) * bins);
    if (idx >= bins) idx = bins - 1;
    counts[idx]++;
  }});
  const maxCount = Math.max(...counts);
  const bars = counts.map(c => `<div style="height:${{Math.max(4, (c / maxCount) * 56)}}px" title="${{c}} ilan"></div>`).join('');
  return `<div class="hist">${{bars}}</div><div style="font-size:.7rem; opacity:.6;">${{Math.round(min)}} m² – ${{Math.round(max)}} m²</div>`;
}}

function renderDistrictTable() {{
  const body = document.getElementById('district-body');
  body.innerHTML = '';
  DISTRICT_STATS.sort((a, b) => a.district.localeCompare(b.district) || a.categoryLabel.localeCompare(b.categoryLabel));
  DISTRICT_STATS.forEach(s => {{
    const tr = document.createElement('tr');
    tr.className = 'district-row';
    tr.innerHTML = `
      <td>${{s.district}}</td>
      <td>${{s.categoryLabel}}</td>
      <td>${{s.count}}</td>
      <td>${{ppmStr(s.medianPpm)}}</td>
      <td>${{s.medianM2 || '—'}} m²</td>
      <td>${{histogramSvg(s.m2Values)}}</td>
    `;
    tr.addEventListener('click', () => {{
      state.district = s.district;
      state.category = s.category;
      document.getElementById('f-district').value = s.district;
      document.getElementById('f-category').value = s.category;
      renderList();
      document.getElementById('listing-body').scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    }});
    body.appendChild(tr);
  }});
}}

// --- Harita ---
if (MAHALLE_POINTS.length) {{
  const map = L.map('map').setView([MAHALLE_POINTS[0].lat, MAHALLE_POINTS[0].lon], 9);
  L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '© OpenStreetMap katkıda bulunanlar',
  }}).addTo(map);
  const ppmAll = MAHALLE_POINTS.map(p => p.medianPpm).filter(Boolean);
  const maxPpm = Math.max(...ppmAll, 1);
  MAHALLE_POINTS.forEach(p => {{
    const ratio = p.medianPpm ? p.medianPpm / maxPpm : 0.3;
    const color = `hsl(${{Math.round(130 - ratio * 130)}}, 70%, 45%)`;
    const radius = 6 + Math.min(p.count, 10) * 1.5;
    const marker = L.circleMarker([p.lat, p.lon], {{
      radius, color, fillColor: color, fillOpacity: 0.7, weight: 1,
    }}).addTo(map);
    marker.bindPopup(`
      <b>${{p.name}}</b><br>
      ${{p.count}} ilan · Medyan m² fiyatı: ${{ppmStr(p.medianPpm)}}<br>
      <a href="#" onclick="document.getElementById('f-district').value='${{p.district}}'; document.getElementById('f-district').dispatchEvent(new Event('change')); return false;">Bu ilçenin ilanlarını listele</a>
    `);
  }});
}} else {{
  document.getElementById('map').innerHTML = '<p style="opacity:.6; padding:1rem;">Henüz haritalanabilen mahalle yok.</p>';
}}

// --- Filtre event listener'ları ---
document.getElementById('f-search').addEventListener('input', e => {{ state.search = e.target.value; renderList(); }});
document.getElementById('f-district').addEventListener('change', e => {{ state.district = e.target.value; renderList(); }});
document.getElementById('f-category').addEventListener('change', e => {{ state.category = e.target.value; renderList(); }});
document.getElementById('f-owner').addEventListener('change', e => {{ state.owner = e.target.value; renderList(); }});
document.getElementById('f-sort').addEventListener('change', e => {{ state.sort = e.target.value; renderList(); }});
document.getElementById('f-clear').addEventListener('click', () => {{
  state.search = ''; state.district = ''; state.category = ''; state.owner = ''; state.sort = 'score';
  document.getElementById('f-search').value = '';
  document.getElementById('f-district').value = '';
  document.getElementById('f-category').value = '';
  document.getElementById('f-owner').value = '';
  document.getElementById('f-sort').value = 'score';
  renderList();
}});

renderDistrictTable();
renderList();
</script>
"""
    DASHBOARD_PATH.write_text(html, encoding="utf-8")
    log(
        f"Dashboard güncellendi: {DASHBOARD_PATH} ({deal_count} fırsat, en yüksek puan {top_score}, "
        f"{len(mahalle_points)}/{len(mahalle_groups)} mahalle haritalandı)"
    )


def district_of(listing):
    loc = listing.get("location") or ""
    if "," in loc:
        return loc.split(",")[-1].strip()
    return listing.get("location_slug", "Bilinmeyen")


def full_report(config, dry_run=False):
    """Mevcut TÜM eşleşen ilanları (yeni/eski fark etmeksizin) ilçe ilçe gruplanmış
    topic'lere, HER İLAN AYRI MESAJ olacak şekilde gönderir (link önizlemesi/fotoğraf
    görünsün diye). Ayrıca hepsini seen.db'ye işler ki normal çalışmada tekrar
    'yeni' sayılmasınlar.

    Aynı ilan kısa süre içinde tekrar tekrar bildirilmesin diye 'soğuma süresi'
    (full_report_cooldown_hours, varsayılan 12 saat) uygulanır: bir ilan bu süre
    içinde zaten bildirildiyse tekrar mesaj atılmaz (full_report art arda birden
    fazla kez çalıştırılsa bile aynı ilan spam gibi tekrar tekrar gönderilmez)."""
    conn = init_db()
    matches = collect_all_matches(config)
    cooldown_hours = config.get("full_report_cooldown_hours", 12)

    groups = {}
    for l in matches:
        groups.setdefault(district_of(l), []).append(l)

    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    now_ts = time.time()

    for l in matches:
        conn.execute(
            """INSERT INTO seen (id, category, location, first_seen, last_price)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET last_price = excluded.last_price""",
            (l["id"], l["category"], l["location_slug"], now_str, l["price"]),
        )
        log_price_history(conn, l["id"], l["price"])
    conn.commit()

    use_forum = forum_configured(config)
    forum_chat_id = config["telegram"].get("forum_chat_id") if use_forum else None

    sent_count = 0
    skipped_count = 0

    for district in sorted(groups.keys()):
        thread_id = None
        if use_forum:
            thread_id = get_topic_thread_id(conn, config, district, dry_run=dry_run)
        for listing in groups[district]:
            row = conn.execute(
                "SELECT last_notified FROM seen WHERE id = ?", (listing["id"],)
            ).fetchone()
            if row and row[0]:
                last_notified_ts = time.mktime(time.strptime(row[0], "%Y-%m-%d %H:%M:%S"))
                if now_ts - last_notified_ts < cooldown_hours * 3600:
                    skipped_count += 1
                    continue

            send_telegram(
                config,
                format_message(listing),
                dry_run=dry_run,
                chat_id=forum_chat_id if use_forum else None,
                thread_id=thread_id,
            )
            conn.execute(
                "UPDATE seen SET last_notified = ? WHERE id = ?", (now_str, listing["id"])
            )
            conn.commit()
            sent_count += 1
            time.sleep(2.5)

    generate_dashboard(conn, matches)
    conn.close()
    log(
        f"Tam rapor: {len(groups)} ilçe, {len(matches)} ilan, {sent_count} mesaj gönderildi, "
        f"{skipped_count} soğuma süresi nedeniyle atlandı."
    )


def main():
    dry_run = "--dry-run" in sys.argv
    config = load_config()

    if "--full-report" in sys.argv:
        full_report(config, dry_run=dry_run)
        return

    conn = init_db()
    first_run = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0
    silent_first_run = config.get("silent_first_run", False)
    use_forum = forum_configured(config)
    forum_chat_id = config["telegram"].get("forum_chat_id") if use_forum else None

    total_new = 0
    total_checked = 0
    total_price_changes = 0
    dashboard_matches = []

    for location in config["locations"]:
        for category in config["categories"]:
            try:
                matches = fetch_category_listings(config, category, location)
            except Exception as e:
                log(f"HATA: {category}/{location} -> {e}")
                continue

            total_checked += len(matches)

            for listing in matches:
                row = conn.execute(
                    "SELECT last_price FROM seen WHERE id = ?", (listing["id"],)
                ).fetchone()

                if row:
                    # Daha önce görülmüş bir ilan - fiyat değişmiş mi diye bak.
                    old_price = row[0]
                    new_price = listing["price"]
                    if (
                        new_price is not None
                        and old_price is not None
                        and new_price != old_price
                    ):
                        conn.execute(
                            "UPDATE seen SET last_price = ? WHERE id = ?",
                            (new_price, listing["id"]),
                        )
                        conn.commit()
                        log_price_history(conn, listing["id"], new_price)
                        total_price_changes += 1

                        if not (first_run and silent_first_run):
                            thread_id = None
                            if use_forum:
                                thread_id = get_topic_thread_id(
                                    conn, config, district_of(listing), dry_run=dry_run
                                )
                            send_telegram(
                                config,
                                format_price_change_message(listing, old_price, new_price),
                                dry_run=dry_run,
                                chat_id=forum_chat_id if use_forum else None,
                                thread_id=thread_id,
                            )
                            conn.execute(
                                "UPDATE seen SET last_notified = ? WHERE id = ?",
                                (time.strftime("%Y-%m-%d %H:%M:%S"), listing["id"]),
                            )
                            conn.commit()
                            time.sleep(1)
                    if passes_filters(listing, config):
                        dashboard_matches.append(listing)
                    continue

                if not passes_filters(listing, config):
                    continue

                conn.execute(
                    "INSERT INTO seen (id, category, location, first_seen, last_price) VALUES (?, ?, ?, ?, ?)",
                    (
                        listing["id"],
                        category,
                        location,
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        listing["price"],
                    ),
                )
                conn.commit()
                log_price_history(conn, listing["id"], listing["price"])
                total_new += 1
                dashboard_matches.append(listing)

                if first_run and silent_first_run:
                    continue

                thread_id = None
                if use_forum:
                    thread_id = get_topic_thread_id(
                        conn, config, district_of(listing), dry_run=dry_run
                    )
                send_telegram(
                    config,
                    format_message(listing),
                    dry_run=dry_run,
                    chat_id=forum_chat_id if use_forum else None,
                    thread_id=thread_id,
                )
                conn.execute(
                    "UPDATE seen SET last_notified = ? WHERE id = ?",
                    (time.strftime("%Y-%m-%d %H:%M:%S"), listing["id"]),
                )
                conn.commit()
                time.sleep(1)  # Telegram flood limitine takılmamak için

            time.sleep(config.get("request_delay_seconds", 1.5))

    generate_dashboard(conn, dashboard_matches)
    conn.close()
    log(
        f"Tarama bitti. {total_checked} ilan kontrol edildi, {total_new} yeni eşleşme, "
        f"{total_price_changes} fiyat değişikliği bulundu."
        + (" (ilk çalıştırma, DB dolduruldu)" if first_run else "")
    )


if __name__ == "__main__":
    main()
