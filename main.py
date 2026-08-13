import os
import re
import time
import requests
import gspread
from google.oauth2.service_account import Credentials

# =============================
# AUTH (GOOGLE SERVICE ACCOUNT)
# =============================
# Thiết lập quyền hạn truy cập Google Sheets & Google Drive
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# Kiểm tra xem đang chạy trên GitHub Actions (dùng Environment Variable) 
# hay chạy local (dùng file json local)
SERVICE_ACCOUNT_FILE = "service_account.json"

if os.path.exists(SERVICE_ACCOUNT_FILE):
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
else:
    # Lấy thông tin từ GitHub Secrets (đã được cấu hình ở Bước 3)
    creds = Credentials.from_service_account_info(
        eval(os.environ["GCP_SA_KEY"]), scopes=SCOPES
    )

gc = gspread.authorize(creds)
print("Google Auth OK")

# =============================
# SHEETS
# =============================
MAIN_SHEET_ID = "1F0dU6uN3kDH1y_VYFJCyJOZpqLWGaTM6WsmnglG77qk"
KEY_SHEET_ID  = "1wzgeUWKlXe-QU-rDZLaLjIQxeXreNvbm3Fi88UZjXWM"

main_sheet = gc.open_by_key(MAIN_SHEET_ID).sheet1
key_spread = gc.open_by_key(KEY_SHEET_ID)

full_sheet = key_spread.worksheet("Fullenrich")
print("Sheets Connected")

# =============================
# API & SETTINGS
# =============================
FULL_START  = "https://app.fullenrich.com/api/v1/contact/enrich/bulk"
FULL_RESULT = "https://app.fullenrich.com/api/v1/contact/enrich/bulk/"

TIMEOUT = 90
POLL_INTERVAL = 3

# =============================
# HELPERS
# =============================
def is_valid_email(email):
    return bool(re.match(r"[^@]+@[^@]+\.[^@]+", str(email)))

def get_full_key():
    rows = full_sheet.get_all_values()
    for i in range(1, len(rows)):  # Python index tính từ 0
        key = rows[i][3] if len(rows[i]) >= 4 else ""
        status = rows[i][4] if len(rows[i]) >= 5 else ""

        if key and status == "":
            return key, i + 1  # gspread tính dòng từ 1
    return None, None

def mark_full_dead(row):
    full_sheet.update_cell(row, 5, "Hết lượt")

# =============================
# SINGLE ENRICH
# =============================
def enrich_single(linkedin):
    while True:
        key, row = get_full_key()
        print("Key Row:", row)

        if not key:
            print("❌ No FullEnrich keys left")
            return "No Key"

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }

        payload = {
            "name": "linkedin_single",
            "datas": [
                {
                    "linkedin_url": linkedin,
                    "enrich_fields": ["contact.emails"]
                }
            ]
        }

        r = requests.post(FULL_START, headers=headers, json=payload)

        if r.status_code == 401:
            mark_full_dead(row)
            continue

        if r.status_code != 200:
            print("Start error:", r.text)
            time.sleep(5)
            continue

        enrich_id = r.json().get("enrichment_id")
        start = time.time()

        while True:
            rr = requests.get(
                FULL_RESULT + enrich_id,
                headers={"Authorization": f"Bearer {key}"}
            )

            data = rr.json()
            status = data.get("status", "").lower()
            print("Status:", status)

            if status in ["credits_insufficient", "payment_required"]:
                mark_full_dead(row)
                break

            if status in ["finished", "completed", "success"]:
                try:
                    item = data["datas"][0]
                    emails = item.get("contact", {}).get("emails", [])
                    if not emails:
                        emails = item.get("contact", {}).get("personal_emails", [])

                    if emails:
                        email = emails[0]["email"]
                        if is_valid_email(email):
                            return email
                except Exception as e:
                    print("Error parsing email:", e)

                return "- Not Found"

            if time.time() - start > TIMEOUT:
                print("Timeout → final check")
                try:
                    item = data["datas"][0]  # Sửa lỗi truy cập danh sách từ code cũ
                    emails = item.get("contact", {}).get("emails", [])
                    if not emails:
                        emails = item.get("contact", {}).get("personal_emails", [])

                    if emails:
                        email = emails[0]["email"]
                        if is_valid_email(email):
                            return email
                except Exception as e:
                    print("Error in timeout parse:", e)

                return "- Not Found"

            time.sleep(POLL_INTERVAL)

# =============================
# MAIN PROCESS
# =============================
def main():
    rows = main_sheet.get_all_values()
    print("Profiles loaded:", len(rows) - 1)

    for i, row in enumerate(rows[1:], start=2):
        linkedin = row[0] if len(row) > 0 else ""
        email = row[1] if len(row) > 1 else ""

        if not linkedin:
            continue

        if email and email not in ["", "- Not Found"]:
            continue

        print(f"\nProcessing row {i}")
        print("LinkedIn:", linkedin)

        result = enrich_single(linkedin)
        main_sheet.update_cell(i, 2, result)

        print("Result:", result)
        time.sleep(2)

    print("\nDONE")

if __name__ == "__main__":
    main()
