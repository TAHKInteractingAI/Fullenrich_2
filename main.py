import os
import re
import time
import requests
import gspread
from google.oauth2.service_account import Credentials

# =============================
# SETTINGS & FLAGS
# =============================
# Đặt DRY_RUN = True nếu muốn test toàn bộ luồng mà KHÔNG tiêu tốn credit FullEnrich
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "false"

MAIN_SHEET_ID = "1F0dU6uN3kDH1y_VYFJCyJOZpqLWGaTM6WsmnglG77qk"
KEY_SHEET_ID  = "1wzgeUWKlXe-QU-rDZLaLjIQxeXreNvbm3Fi88UZjXWM"

FULL_START  = "https://app.fullenrich.com/api/v1/contact/enrich/bulk"
FULL_RESULT = "https://app.fullenrich.com/api/v1/contact/enrich/bulk/"

TIMEOUT = 120
POLL_INTERVAL = 5
BATCH_SIZE = 60  # Số profile xử lý trong 1 lần gửi API

# =============================
# AUTH
# =============================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

SERVICE_ACCOUNT_FILE = "service_account.json"
if os.path.exists(SERVICE_ACCOUNT_FILE):
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
else:
    creds = Credentials.from_service_account_info(
        eval(os.environ["GCP_SA_KEY"]), scopes=SCOPES
    )

gc = gspread.authorize(creds)
main_sheet = gc.open_by_key(MAIN_SHEET_ID).sheet1
key_sheet = gc.open_by_key(KEY_SHEET_ID).worksheet("Fullenrich")
print(f" Connected Sheets | DRY_RUN = {DRY_RUN}")

# =============================
# KEY MANAGER
# =============================
class KeyManager:
    def __init__(self, worksheet):
        self.ws = worksheet
        self.keys = []
        self.load_keys()

    def load_keys(self):
        rows = self.ws.get_all_values()
        for idx, row in enumerate(rows[1:], start=2):
            key = row[3].strip() if len(row) >= 4 else ""
            status = row[4].strip() if len(row) >= 5 else ""
            if key and status == "":
                self.keys.append({"key": key, "row": idx})
        print(f" Loaded {len(self.keys)} active API keys.")

    def get_current_key(self):
        return self.keys[0] if self.keys else None

    def mark_current_dead(self, reason="Hết lượt"):
        if not self.keys:
            return
        dead = self.keys.pop(0)
        print(f"⚠️ Marking key at row {dead['row']} as '{reason}'")
        self.ws.update_cell(dead["row"], 5, reason)

# =============================
# ENRICHMENT LOGIC (BULK)
# =============================
def is_valid_email(email):
    return bool(re.match(r"[^@]+@[^@]+\.[^@]+", str(email)))

def enrich_batch(linkedin_urls, key_mgr):
    if DRY_RUN:
        print(f" [DRY_RUN] Giả lập tìm {len(linkedin_urls)} URLs thành công.")
        time.sleep(2)
        return {url: "mock_test@example.com" for url in linkedin_urls}

    while True:
        curr = key_mgr.get_current_key()
        if not curr:
            print("❌ Không còn API key khả dụng nào!")
            return None

        headers = {
            "Authorization": f"Bearer {curr['key']}",
            "Content-Type": "application/json"
        }

        payload = {
            "name": f"batch_{int(time.time())}",
            "datas": [{"linkedin_url": url, "enrich_fields": ["contact.emails"]} for url in linkedin_urls]
        }

        r = requests.post(FULL_START, headers=headers, json=payload)
        
        if r.status_code == 401:
            key_mgr.mark_current_dead("Hết hạn/Sai Key")
            continue
        
        if r.status_code != 200:
            print(" Lỗi gửi Batch request:", r.text)
            time.sleep(5)
            continue

        enrich_id = r.json().get("enrichment_id")
        start_time = time.time()
        results = {}

        # Polling kết quả
        while True:
            rr = requests.get(FULL_RESULT + enrich_id, headers={"Authorization": f"Bearer {curr['key']}"})
            data = rr.json()
            status = data.get("status", "").lower()
            print(f"Status Bulk ({len(linkedin_urls)} items): {status}")

            if status in ["credits_insufficient", "payment_required"]:
                key_mgr.mark_current_dead("Hết lượt")
                break  # Thoát vòng lặp polling để thử key tiếp theo

            if status in ["finished", "completed", "success"]:
                items = data.get("datas", [])
                for item in items:
                    url = item.get("linkedin_url")
                    emails = item.get("contact", {}).get("emails", []) or item.get("contact", {}).get("personal_emails", [])
                    found_email = "- Not Found"
                    if emails:
                        e = emails[0].get("email", "")
                        if is_valid_email(e):
                            found_email = e
                    results[url] = found_email
                return results

            if time.time() - start_time > TIMEOUT:
                print("⏱ Timeout khi chờ kết quả batch.")
                return {url: "- Not Found" for url in linkedin_urls}

            time.sleep(POLL_INTERVAL)

# =============================
# MAIN RUNNER
# =============================
def main():
    key_mgr = KeyManager(key_sheet)
    if not key_mgr.keys and not DRY_RUN:
        print(" Không có key để chạy.")
        return

    all_rows = main_sheet.get_all_values()
    pending_items = []

    # Gom các dòng cần xử lý
    for idx, row in enumerate(all_rows[1:], start=2):
        linkedin = row[0].strip() if len(row) > 0 else ""
        email = row[1].strip() if len(row) > 1 else ""

        if linkedin and email in ["", "- Not Found"]:
            pending_items.append({"row": idx, "url": linkedin})

    print(f" Cần tìm email cho: {len(pending_items)} profiles")

    # Xử lý theo từng nhóm (Batch)
    for i in range(0, len(pending_items), BATCH_SIZE):
        batch = pending_items[i : i + BATCH_SIZE]
        urls = [b["url"] for b in batch]
        
        print(f"\n🚀 Đang xử lý batch {i // BATCH_SIZE + 1} ({len(urls)} profile)...")
        res_map = enrich_batch(urls, key_mgr)

        if res_map is None:
            print("⛔ Dừng tiến trình do hết API Key.")
            break

        # Batch update lên Google Sheet (Tiết kiệm Quota)
        cell_updates = []
        for b in batch:
            result_email = res_map.get(b["url"], "- Not Found")
            cell_updates.append({
                "range": f"B{b['row']}",
                "values": [[result_email]]
            })
        
        main_sheet.batch_update(cell_updates)
        print(f" Cập nhật xong batch vào Sheet.")
        time.sleep(2)

    print("\n Hoàn thành toàn bộ!")

if __name__ == "__main__":
    main()
