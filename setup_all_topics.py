#!/usr/bin/env python3
"""
Tüm 45 ilçe için Telegram forum topic'lerini pre-create eder.
Zaten topics.json'da olan ilçeler atlanır.

Çalıştırma (tek sefer, yerel):
  python3 setup_all_topics.py
"""
import json
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SECRETS_PATH = BASE_DIR / "secrets.local.json"
TOPICS_PATH = BASE_DIR / "topics.json"

DISTRICT_NAMES = {
    "izmir-aliaga": "Aliağa",
    "izmir-balcova": "Balçova",
    "izmir-bayindir": "Bayındır",
    "izmir-bayrakli": "Bayraklı",
    "izmir-bergama": "Bergama",
    "izmir-beydag": "Beydağ",
    "izmir-bornova": "Bornova",
    "izmir-buca": "Buca",
    "izmir-cesme": "Çeşme",
    "izmir-cigli": "Çiğli",
    "izmir-dikili": "Dikili",
    "izmir-foca": "Foça",
    "izmir-gaziemir": "Gaziemir",
    "izmir-guzelbahce": "Güzelbahçe",
    "izmir-karabaglar": "Karabağlar",
    "izmir-karaburun": "Karaburun",
    "izmir-karsiyaka": "Karşıyaka",
    "izmir-kemalpasa": "Kemalpaşa",
    "izmir-kinik": "Kınık",
    "izmir-kiraz": "Kiraz",
    "izmir-konak": "Konak",
    "izmir-menderes": "Menderes",
    "izmir-menemen": "Menemen",
    "izmir-narlidere": "Narlıdere",
    "izmir-odemis": "Ödemiş",
    "izmir-seferihisar": "Seferihisar",
    "izmir-selcuk": "Selçuk",
    "izmir-tire": "Tire",
    "izmir-torbali": "Torbalı",
    "izmir-urla": "Urla",
    "manisa-turgutlu": "Turgutlu",
    "manisa-saruhanli": "Saruhanlı",
    "manisa-ahmetli": "Ahmetli",
    "manisa-golmarmara": "Gölmarmara",
    "balikesir-ayvalik": "Ayvalık",
    "balikesir-burhaniye": "Burhaniye",
    "balikesir-edremit": "Edremit",
    "balikesir-gomec": "Gömeç",
    "balikesir-havran": "Havran",
    "balikesir-sindirgi": "Sındırgı",
    "aydin-kusadasi": "Kuşadası",
    "aydin-soke": "Söke",
    "aydin-germencik": "Germencik",
    "aydin-incirliova": "İncirliova",
    "aydin-sultanhisar": "Sultanhisar",
}

# Yanlışlıkla açılmış duplicate topic'ler — silinecek
DUPLICATES_TO_DELETE = [588]  # Tire'nin yanlış kopyası


def api(token, method, payload, retries=5):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.request.HTTPError as e:
            if e.code == 429:
                try:
                    retry_after = json.loads(e.read())["parameters"]["retry_after"]
                except Exception:
                    retry_after = 30
                wait = retry_after + 2
                print(f"  429 rate limit, {wait}sn bekleniyor...")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"API çağrısı {retries} denemede başarısız: {method}")


def main():
    secrets = json.loads(SECRETS_PATH.read_text())
    token = secrets["telegram"]["bot_token"]
    forum_chat_id = secrets["telegram"]["forum_chat_id"]

    # Mevcut topics.json'u yükle
    existing = {}
    if TOPICS_PATH.exists():
        existing = json.loads(TOPICS_PATH.read_text())

    # --- Tire'yi doğru ID'ye sabitle ---
    existing["Tire"] = 512
    print("✅  Tire → 512 (doğru ID)")

    # --- Duplicate topic'leri sil ---
    active_ids = set(existing.values())
    for dup_id in DUPLICATES_TO_DELETE:
        if dup_id in active_ids:
            print(f"⚠️   {dup_id} aktif topics'te zaten var, silme atlandı.")
            continue
        try:
            result = api(
                token,
                "deleteForumTopic",
                {"chat_id": forum_chat_id, "message_thread_id": dup_id},
            )
            if result.get("result"):
                print(f"🗑️   Duplicate {dup_id} silindi.")
            else:
                result2 = api(
                    token,
                    "closeForumTopic",
                    {"chat_id": forum_chat_id, "message_thread_id": dup_id},
                )
                if result2.get("result"):
                    print(f"🔒  Duplicate {dup_id} kapatıldı (içinde mesaj var, silinemedi).")
                else:
                    print(f"❌  {dup_id} işlenemedi: {result}")
        except Exception as e:
            print(f"❌  {dup_id} silerken hata: {e}")

    # --- Eksik ilçeleri oluştur ---
    created = 0
    skipped = 0
    for slug, district_name in DISTRICT_NAMES.items():
        if district_name in existing:
            print(f"⏭️   {district_name} zaten var (thread_id={existing[district_name]})")
            skipped += 1
            continue

        try:
            result = api(
                token,
                "createForumTopic",
                {"chat_id": forum_chat_id, "name": district_name},
            )
            thread_id = result["result"]["message_thread_id"]
            existing[district_name] = thread_id
            print(f"✅  {district_name} oluşturuldu → thread_id={thread_id}")
            created += 1
            time.sleep(0.5)
        except Exception as e:
            print(f"❌  {district_name} oluşturulamadı: {e}")

    # --- topics.json'u kaydet ---
    TOPICS_PATH.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"\nTamamlandı: {created} yeni topic açıldı, {skipped} mevcut atlandı."
        "\ntopics.json güncellendi — commit etmeyi unutma."
    )


if __name__ == "__main__":
    main()
