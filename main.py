import requests
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.http import MediaIoBaseUpload
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from datetime import datetime, date
from zoneinfo import ZoneInfo
import io
import os
import time
from dotenv import load_dotenv

# =========================
# LOAD ENV
# =========================
load_dotenv()

IMAGES_PER_DAY = 7
SCOPES = ['https://www.googleapis.com/auth/drive']


# =========================
# LOAD ANIMALS
# =========================
def load_animals():
    url = "https://gist.githubusercontent.com/EyeOfMidas/311e77b8b8c2f334fc8bdaf652c1f47f/raw"
    res = requests.get(url, timeout=20)
    res.raise_for_status()

    animals = []
    seen = set()

    for line in res.text.split("\n"):
        if not line.strip():
            continue

        clean = line.split(",", 1)[-1].strip().lower()
        if clean and clean not in seen:
            animals.append(clean)
            seen.add(clean)

    print(f"✅ Loaded {len(animals)} animals")
    return animals


# =========================
# GOOGLE DRIVE
# =========================
def authenticate_drive():
    creds = None

    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired:
            print("🔄 Refreshing token...")
            creds.refresh(Request())
        else:
            raise ValueError("❌ token.json missing")

    return build("drive", "v3", credentials=creds)


# =========================
# MONGO
# =========================
def get_collection():
    uri = os.environ.get("MONGO_URI")
    if not uri:
        raise ValueError("❌ MONGO_URI missing")

    client = MongoClient(uri)
    db = client["PetProject_DB"]
    col = db["images_metadata"]

    col.create_index("pixabay_id", unique=True)

    return col


# =========================
# FOLDER
# =========================
def get_or_create_folder(service, name, parent_id):
    res = service.files().list(
        q=f"'{parent_id}' in parents and trashed=false and mimeType='application/vnd.google-apps.folder'",
        fields="files(id,name)"
    ).execute()

    for f in res.get("files", []):
        if f["name"].lower() == name.lower():
            return f["id"]

    folder = service.files().create(
        body={
            "name": name,
            "parents": [parent_id],
            "mimeType": "application/vnd.google-apps.folder"
        },
        fields="id"
    ).execute()

    return folder["id"]


# =========================
# MAIN
# =========================
def main():
    print("\n🚀 JOB STARTED\n")

    start = datetime.now()

    # ✅ FIXED DATE (INDIA)
    today_str = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")

    total_uploaded = 0
    total_skipped = 0
    total_no_results = 0

    try:
        animals = load_animals()

        api_key = os.environ.get("PIXABAY_KEY")
        drive_folder_id = os.environ.get("DRIVE_FOLDER_ID")

        if not api_key:
            raise ValueError("❌ PIXABAY_KEY missing")

        drive = authenticate_drive()
        collection = get_collection()

        # ✅ FIXED PAGE LOGIC
        start_date = date(2025, 12, 24)
        days = (date.today() - start_date).days
        page = (days % 10) + 1   # 🔥 SAFE

        print(f"📅 Using Pixabay Page: {page}")

        for animal in animals:
            print(f"\n🐾 {animal}")

            try:
                url = (
                    f"https://pixabay.com/api/"
                    f"?key={api_key}"
                    f"&q={animal}"
                    f"&image_type=photo"
                    f"&per_page={IMAGES_PER_DAY}"
                    f"&page={page}"
                )

                res = requests.get(url, timeout=20)

                # ✅ HANDLE ERRORS PROPERLY
                if res.status_code == 400:
                    print("   ⚠️ Bad request → skipping")
                    continue

                if res.status_code == 429:
                    print("   ⏳ Rate limit → stopping job")
                    break

                res.raise_for_status()

                hits = res.json().get("hits", [])

                if not hits:
                    print("   ⚠️ No images")
                    total_no_results += 1
                    continue

                folder_id = get_or_create_folder(
                    drive, animal.title(), drive_folder_id
                )

                uploaded_here = 0

                for hit in hits:
                    pixabay_id = hit["id"]

                    # ✅ Mongo duplicate check ONLY
                    if collection.find_one({"pixabay_id": pixabay_id}):
                        total_skipped += 1
                        continue

                    filename = f"{animal.replace(' ','_')}_{pixabay_id}.jpg"

                    print(f"   ⬇️ {filename}")

                    img = requests.get(hit["webformatURL"], timeout=30).content

                    media = MediaIoBaseUpload(
                        io.BytesIO(img),
                        mimetype="image/jpeg"
                    )

                    file = drive.files().create(
                        body={
                            "name": filename,
                            "parents": [folder_id]
                        },
                        media_body=media,
                        fields="webViewLink"
                    ).execute()

                    doc = {
                        "name": animal,
                        "pixabay_id": pixabay_id,
                        "animal_type": animal,
                        "tags": hit.get("tags", ""),
                        "photographer": hit.get("user", ""),
                        "google_drive_url": file.get("webViewLink"),
                        "date_added": today_str,
                        "original_source": hit.get("pageURL", "")
                    }

                    try:
                        collection.insert_one(doc)
                        total_uploaded += 1
                        uploaded_here += 1
                    except DuplicateKeyError:
                        pass

                    time.sleep(0.2)

                print(f"   ✅ Uploaded: {uploaded_here}")

            except Exception as e:
                print(f"   ❌ Error: {e}")

        # =========================
        # SUMMARY
        # =========================
        print("\n==============================")
        print("🎉 DONE")
        print("==============================")
        print(f"📅 Date: {today_str}")
        print(f"🖼️ Uploaded: {total_uploaded}")
        print(f"⏭️ Skipped: {total_skipped}")
        print(f"⚠️ No Results: {total_no_results}")
        print(f"⏱️ Time: {datetime.now() - start}")

    except Exception as e:
        print(f"\n❌ CRITICAL ERROR: {e}")


if __name__ == "__main__":
    main()
