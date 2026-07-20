#!/usr/bin/env python3
"""
Telegram forum'daki duplicate (çift açılmış) topic'leri kapatır/siler.

Kullanım:
  python3 close_duplicate_topics.py 123 456 789

  Argümanlar: silinecek ESKİ topic thread_id'leri (boşlukla ayrılmış)

Aktif topic'leri görmek için:
  python3 close_duplicate_topics.py --list
"""
import json, sys, urllib.request, urllib.parse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SECRETS_PATH = BASE_DIR / "secrets.local.json"
TOPICS_PATH  = BASE_DIR / "topics.json"

def api(token, method, payload):
    url  = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def main():
    secrets = json.loads(SECRETS_PATH.read_text())
    token   = secrets["telegram"]["bot_token"]
    chat_id = secrets["telegram"]["forum_chat_id"]

    if "--list" in sys.argv:
        if TOPICS_PATH.exists():
            active = json.loads(TOPICS_PATH.read_text())
            print("Aktif topic'ler (bunlara DOKUNMA):")
            for district, tid in sorted(active.items()):
                print(f"  {district}: thread_id={tid}")
        else:
            print("topics.json bulunamadı.")
        return

    ids_to_delete = []
    for arg in sys.argv[1:]:
        try:
            ids_to_delete.append(int(arg))
        except ValueError:
            print(f"Geçersiz ID atlandı: {arg}")

    if not ids_to_delete:
        print(__doc__)
        sys.exit(1)

    active_ids = set()
    if TOPICS_PATH.exists():
        active_ids = set(json.loads(TOPICS_PATH.read_text()).values())

    for tid in ids_to_delete:
        if tid in active_ids:
            print(f"⚠️  {tid} aktif topic'lerde var, silmiyorum.")
            continue
        try:
            result = api(token, "deleteForumTopic", {"chat_id": chat_id, "message_thread_id": tid})
            if result.get("result"):
                print(f"✅  {tid} silindi.")
            else:
                # Silme başarısız — önce kapat dene
                result2 = api(token, "closeForumTopic", {"chat_id": chat_id, "message_thread_id": tid})
                if result2.get("result"):
                    print(f"🔒  {tid} kapatıldı (silinemedi — içinde mesaj var olabilir).")
                else:
                    print(f"❌  {tid} işlenemedi: {result}")
        except Exception as e:
            print(f"❌  {tid} hata: {e}")

if __name__ == "__main__":
    main()
