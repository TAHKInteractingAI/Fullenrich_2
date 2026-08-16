import os
import re
import time
import requests
import gspread
from google.oauth2.service_account import Credentials

# =============================
# SETTINGS & FLAGS
# =============================
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

MAIN_SHEET_ID = "1F0dU6uN3kDH1y_VYFJCyJOZpqLWGaTM6WsmnglG77qk"
KEY_SHEET_ID  = "1wzgeUWKlXe-QU-rDZLaLjIQxeXreNvbm3Fi88UZjXWM"

FULL_START  = "https://app.fullenrich.com/api/v1/contact/enrich/bulk"
FULL_RESULT = "https://app.fullenrich.com/api/v1/contact/enrich/bulk/"

TIMEOUT = 120
POLL_INTERVAL = 5
BATCH_SIZE = 50

# Số lần thử lại tối đa khi gửi request hoặc poll bị lỗi (tránh loop vô hạn -> spam key)
MAX_SEND_RETRIES = 3
MAX_POLL_ERRORS = 3

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
print(f"Connected Sheets | DRY_RUN = {DRY_RUN}")

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
        print(f"Loaded {len(self.keys)} active API keys.")

    def get_current_key(self):
        return self.keys[0] if self.keys else None

    def mark_current_dead(self, reason="Hết lượt"):
        if not self.keys:
            return
        dead = self.keys.pop(0)
        print(f"⚠️ Marking key at row {dead['row']} as '{reason}'")
        self.ws.update_cell(dead["row"], 5, reason)

# =============================
# ENRICHMENT LOGIC (BULK - FIXED)
# =============================
def is_valid_email(email):
    return bool(re.match(r"[^@]+@[^@]+\.[^@]+", str(email)))

def enrich_batch(linkedin_urls, key_mgr):
    if DRY_RUN:
        print(f"[DRY_RUN] Giả lập tìm {len(linkedin_urls)} URLs thành công.")
        time.sleep(2)
        return ["mock_test@example.com"] * len(linkedin_urls)

    send_retry_count = 0

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

        try:
            r = requests.post(FULL_START, headers=headers, json=payload, timeout=30)
        except requests.exceptions.RequestException as e:
            send_retry_count += 1
            print(f"⚠️ Lỗi network khi gửi batch (lần {send_retry_count}/{MAX_SEND_RETRIES}): {e}")
            if send_retry_count >= MAX_SEND_RETRIES:
                print("❌ Vượt quá số lần thử lại cho phép (network). Bỏ qua batch này để tránh spam.")
                return ["- Not Found"] * len(linkedin_urls)
            time.sleep(5 * send_retry_count)
            continue

        if r.status_code == 401:
            key_mgr.mark_current_dead("Hết hạn/Sai Key")
            send_retry_count = 0  # đổi key mới -> reset đếm retry
            continue

        if r.status_code != 200:
            send_retry_count += 1
            print(f"Lỗi gửi Batch request (lần {send_retry_count}/{MAX_SEND_RETRIES}): {r.text}")
            if send_retry_count >= MAX_SEND_RETRIES:
                print("❌ Vượt quá số lần thử lại cho phép. Bỏ qua batch này để tránh spam phí phạm key.")
                return ["- Not Found"] * len(linkedin_urls)
            time.sleep(5 * send_retry_count)
            continue

        enrich_id = r.json().get("enrichment_id")
        start_time = time.time()
        poll_error_count = 0

        # Polling kết quả
        while True:
            try:
                rr = requests.get(FULL_RESULT + enrich_id, headers={"Authorization": f"Bearer {curr['key']}"}, timeout=30)
                data = rr.json()
            except (requests.exceptions.RequestException, ValueError) as e:
                poll_error_count += 1
                print(f"⚠️ Lỗi khi poll kết quả (lần {poll_error_count}/{MAX_POLL_ERRORS}): {e}")
                if poll_error_count >= MAX_POLL_ERRORS:
                    print("❌ Vượt quá số lần thử lại cho phép khi poll. Bỏ qua batch này.")
                    return ["- Not Found"] * len(linkedin_urls)
                time.sleep(POLL_INTERVAL)
                continue

            status = data.get("status", "").lower()
            print(f"Status Bulk ({len(linkedin_urls)} items): {status}")

            if status in ["credits_insufficient", "payment_required"]:
                key_mgr.mark_current_dead("Hết lượt")
                break

            if status in ["finished", "completed", "success"]:
                items = data.get("datas", [])
                results_list = []

                # Duyệt theo đúng thứ tự index của danh sách gửi đi
                for i in range(len(linkedin_urls)):
                    item = items[i] if i < len(items) else {}
                    emails = item.get("contact", {}).get("emails", []) or item.get("contact", {}).get("personal_emails", [])

                    found_email = "- Not Found"
                    if emails:
                        e = emails[0].get("email", "")
                        if is_valid_email(e):
                            found_email = e

                    results_list.append(found_email)

                return results_list

            if time.time() - start_time > TIMEOUT:
                print("⏱ Timeout khi chờ kết quả batch.")
                return ["- Not Found"] * len(linkedin_urls)

            time.sleep(POLL_INTERVAL)

        # Nếu vừa break do hết credit ở trên, quay lại vòng ngoài để thử key kế tiếp
        send_retry_count = 0

# =============================
# MAIN RUNNER (FIXED)
# =============================
def main():
    key_mgr = KeyManager(key_sheet)
    if not key_mgr.keys and not DRY_RUN:
        print("Không có key để chạy.")
        return

    all_rows = main_sheet.get_all_values()
    pending_items = []

    # Gom các dòng cần xử lý
    for idx, row in enumerate(all_rows[1:], start=2):
        linkedin = row[0].strip() if len(row) > 0 else ""
        email = row[1].strip() if len(row) > 1 else ""

        if linkedin and email in ["", "- Not Found"]:
            pending_items.append({"row": idx, "url": linkedin})

    print(f"Cần tìm email cho: {len(pending_items)} profiles")

    # Xử lý theo từng nhóm (Batch) - lặp cho đến khi hết TOÀN BỘ data đang chờ
    for i in range(0, len(pending_items), BATCH_SIZE):
        batch = pending_items[i : i + BATCH_SIZE]
        urls = [b["url"] for b in batch]

        print(f"\n🚀 Đang xử lý batch {i // BATCH_SIZE + 1}/{(len(pending_items) - 1) // BATCH_SIZE + 1} ({len(urls)} profile)...")
        results_list = enrich_batch(urls, key_mgr)

        if results_list is None:
            print("⛔ Dừng tiến trình do hết API Key.")
            break

        # Batch update lên Google Sheet theo đúng thứ tự dòng
        cell_updates = []
        for idx, b in enumerate(batch):
            result_email = results_list[idx]
            cell_updates.append({
                "range": f"B{b['row']}",
                "values": [[result_email]]
            })

        main_sheet.batch_update(cell_updates)
        print(f"Cập nhật xong batch vào Sheet.")
        time.sleep(2)

    print("\nHoàn thành toàn bộ!")

if __name__ == "__main__":
    main()
