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


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


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


def generate_dashboard(matches):
    """matches listesinden ilçe+kategori bazlı özet ve fırsat listesi içeren
    statik bir dashboard.html üretir."""
    groups = {}
    for l in matches:
        key = (district_of(l), l["category"])
        groups.setdefault(key, []).append(l)

    district_rows = []
    for (district, category), listings in sorted(groups.items()):
        ppm_values = [l["price_per_m2"] for l in listings if l.get("price_per_m2")]
        med = statistics.median(ppm_values) if ppm_values else None
        med_str = f"{med:,.0f} TL/m²".replace(",", ".") if med else "—"
        district_rows.append(
            f"<tr><td>{_html_escape(district)}</td>"
            f"<td>{_html_escape(CATEGORY_LABELS.get(category, category))}</td>"
            f"<td>{len(listings)}</td><td>{med_str}</td></tr>"
        )

    deal_count = sum(1 for l in matches if l.get("deal_note"))
    top_scored = sorted(matches, key=lambda l: l.get("investment_score", 0), reverse=True)[:30]

    def listing_card(l):
        price_str = f"{l['price']:,.0f} TL".replace(",", ".") if l["price"] else "—"
        ppm_str = f"{l['price_per_m2']:,.0f} TL/m²".replace(",", ".") if l.get("price_per_m2") else "—"
        owner = "👤 Sahibinden" if l.get("is_sahibinden") else "🏢 Emlak Ofisinden"
        deal = f'<div class="deal">{_html_escape(l["deal_note"])}</div>' if l.get("deal_note") else ""
        score = l.get("investment_score")
        score_badge = f'<div class="score">🤖 {score}/100</div>' if score is not None else ""
        return f"""
        <a class="card" href="{_html_escape(l['url'])}" target="_blank" rel="noopener">
          {score_badge}
          <div class="card-title">{_html_escape(l['name'])}</div>
          <div class="card-meta">{_html_escape(district_of(l))} · {_html_escape(CATEGORY_LABELS.get(l['category'], l['category']))} · {owner}</div>
          <div class="card-price">{price_str} <span class="ppm">({ppm_str})</span></div>
          {deal}
        </a>"""

    deal_cards = "\n".join(listing_card(l) for l in top_scored) or "<p>Şu an hiç ilan yok.</p>"

    html = f"""<title>KöyEviBot Dashboard</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.4rem; }}
  h2 {{ font-size: 1.1rem; margin-top: 2rem; border-bottom: 1px solid #8884; padding-bottom: .3rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
  th, td {{ text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #8883; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: .8rem; }}
  .card {{ display: block; border: 1px solid #8884; border-radius: .6rem; padding: .8rem; text-decoration: none; color: inherit; }}
  .card:hover {{ border-color: #f60; }}
  .card-title {{ font-weight: 600; margin-bottom: .3rem; }}
  .card-meta {{ font-size: .8rem; opacity: .7; margin-bottom: .4rem; }}
  .card-price {{ font-weight: 600; }}
  .ppm {{ font-weight: 400; opacity: .7; font-size: .85rem; }}
  .deal {{ margin-top: .4rem; color: #d9480f; font-weight: 600; font-size: .85rem; }}
  .score {{ display: inline-block; margin-bottom: .3rem; font-size: .8rem; font-weight: 700; color: #7048e8; }}
  .updated {{ opacity: .6; font-size: .85rem; }}
</style>
<h1>🏡 KöyEviBot Dashboard</h1>
<p class="updated">Son güncelleme: {time.strftime('%Y-%m-%d %H:%M:%S')} — {len(matches)} ilan, {deal_count} fırsat</p>

<h2>🤖 AI Yatırım Puanına Göre En İyi 30 İlan (Beta)</h2>
<div class="grid">
{deal_cards}
</div>

<h2>📊 İlçe / Kategori Bazlı Medyan m² Fiyatları</h2>
<table>
<tr><th>İlçe</th><th>Kategori</th><th>İlan Sayısı</th><th>Medyan m² Fiyatı</th></tr>
{"".join(district_rows)}
</table>
"""
    DASHBOARD_PATH.write_text(html, encoding="utf-8")
    log(f"Dashboard güncellendi: {DASHBOARD_PATH} ({deal_count} fırsat, en yüksek puan {top_scored[0]['investment_score'] if top_scored else '-'})")


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

    conn.close()
    generate_dashboard(matches)
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

    conn.close()
    generate_dashboard(dashboard_matches)
    log(
        f"Tarama bitti. {total_checked} ilan kontrol edildi, {total_new} yeni eşleşme, "
        f"{total_price_changes} fiyat değişikliği bulundu."
        + (" (ilk çalıştırma, DB dolduruldu)" if first_run else "")
    )


if __name__ == "__main__":
    main()
