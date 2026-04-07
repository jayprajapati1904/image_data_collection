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
        clean = " ".join(clean.split())  # normalize spaces

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
        if creds and creds.expired and creds.refresh_token:
            print("🔄 Refreshing token...")
            creds.refresh(Request())
        else:
            raise ValueError("❌ token.json missing or invalid")

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

    # Unique index to prevent duplicate Pixabay IDs
    col.create_index("pixabay_id", unique=True)

    return col


# =========================
# NORMALIZE FOLDER NAME
# =========================
def normalize_folder_name(name):
    return " ".join(name.strip().split()).title()


# =========================
# GOOGLE DRIVE FOLDER
# =========================
def get_or_create_folder(service, name, parent_id):
    """
    Find exact folder by name inside given parent.
    If not found, create it.
    This prevents redundant folders.
    """
    name = " ".join(name.strip().split())

    query = (
        f"'{parent_id}' in parents and "
        f"trashed=false and "
        f"mimeType='application/vnd.google-apps.folder' and "
        f"name='{name}'"
    )

    res = service.files().list(
        q=query,
        fields="files(id,name)",
        spaces="drive"
    ).execute()

    files = res.get("files", [])

    if files:
        print(f"📁 Folder exists: {name}")
        return files[0]["id"]

    print(f"📂 Creating folder: {name}")

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
# DOWNLOAD IMAGE
# =========================
def download_image(url):
    res = requests.get(url, timeout=30)
    res.raise_for_status()
    return res.content


# =========================
# UPLOAD TO GOOGLE DRIVE
# =========================
def upload_to_drive(service, folder_id, filename, image_bytes):
    media = MediaIoBaseUpload(
        io.BytesIO(image_bytes),
        mimetype="image/jpeg"
    )

    file = service.files().create(
        body={
            "name": filename,
            "parents": [folder_id]
        },
        media_body=media,
        fields="id, webViewLink"
    ).execute()

    return file


# =========================
# GET PIXABAY IMAGES
# =========================
def fetch_pixabay_images(api_key, animal, page, per_page):
    url = (
        f"https://pixabay.com/api/"
        f"?key={api_key}"
        f"&q={animal}"
        f"&image_type=photo"
        f"&per_page={per_page}"
        f"&page={page}"
    )

    res = requests.get(url, timeout=20)

    if res.status_code == 400:
        print("   ⚠️ Bad request → skipping")
        return None

    if res.status_code == 429:
        print("   ⏳ Rate limit reached")
        return "RATE_LIMIT"

    res.raise_for_status()
    return res.json().get("hits", [])


# =========================
# MAIN
# =========================
def main():
    print("\n🚀 JOB STARTED\n")
    start = datetime.now()

    # India date
    today_str = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")

    total_uploaded = 0
    total_skipped = 0
    total_no_results = 0
    total_errors = 0

    try:
        animals = load_animals()

        api_key = os.environ.get("PIXABAY_KEY")
        drive_folder_id = os.environ.get("DRIVE_FOLDER_ID")

        if not api_key:
            raise ValueError("❌ PIXABAY_KEY missing")

        if not drive_folder_id:
            raise ValueError("❌ DRIVE_FOLDER_ID missing")

        drive = authenticate_drive()
        collection = get_collection()

        # Page rotation logic
        start_date = date(2025, 12, 24)
        days = (date.today() - start_date).days
        page = (days % 10) + 1

        print(f"📅 Using Pixabay Page: {page}")
        print(f"📆 Today (India): {today_str}")

        for animal in animals:
            print(f"\n🐾 Processing: {animal}")

            try:
                hits = fetch_pixabay_images(api_key, animal, page, IMAGES_PER_DAY)

                if hits == "RATE_LIMIT":
                    print("\n⛔ Stopping due to Pixabay rate limit.")
                    break

                if hits is None:
                    total_errors += 1
                    continue

                if not hits:
                    print("   ⚠️ No images found")
                    total_no_results += 1
                    continue

                # Normalize folder name
                folder_name = normalize_folder_name(animal)

                # Get/create Drive folder
                folder_id = get_or_create_folder(drive, folder_name, drive_folder_id)

                uploaded_here = 0

                for hit in hits:
                    try:
                        pixabay_id = hit["id"]

                        # Skip if already in Mongo
                        if collection.find_one({"pixabay_id": pixabay_id}):
                            print(f"   ⏭️ Already exists: {pixabay_id}")
                            total_skipped += 1
                            continue

                        filename = f"{animal.replace(' ', '_')}_{pixabay_id}.jpg"
                        print(f"   ⬇️ Downloading: {filename}")

                        image_bytes = download_image(hit["webformatURL"])

                        print(f"   ☁️ Uploading to Drive: {filename}")
                        uploaded_file = upload_to_drive(
                            drive, folder_id, filename, image_bytes
                        )

                        doc = {
                            "name": animal,
                            "pixabay_id": pixabay_id,
                            "animal_type": animal,
                            "tags": hit.get("tags", ""),
                            "photographer": hit.get("user", ""),
                            "google_drive_url": uploaded_file.get("webViewLink"),
                            "google_drive_file_id": uploaded_file.get("id"),
                            "date_added": today_str,
                            "original_source": hit.get("pageURL", "")
                        }

                        try:
                            collection.insert_one(doc)
                            total_uploaded += 1
                            uploaded_here += 1
                            print(f"   ✅ Uploaded: {filename}")
                        except DuplicateKeyError:
                            print(f"   ⏭️ Duplicate Mongo entry: {pixabay_id}")
                            total_skipped += 1

                        time.sleep(0.2)

                    except Exception as img_err:
                        total_errors += 1
                        print(f"   ❌ Image Error: {img_err}")

                print(f"   🎉 Done for {animal} → Uploaded: {uploaded_here}")

            except Exception as e:
                total_errors += 1
                print(f"   ❌ Animal Error: {e}")

        # =========================
        # SUMMARY
        # =========================
        print("\n==============================")
        print("🎉 JOB COMPLETED")
        print("==============================")
        print(f"📅 Date: {today_str}")
        print(f"🖼️ Uploaded: {total_uploaded}")
        print(f"⏭️ Skipped: {total_skipped}")
        print(f"⚠️ No Results: {total_no_results}")
        print(f"❌ Errors: {total_errors}")
        print(f"⏱️ Time Taken: {datetime.now() - start}")
        print("==============================\n")

    except Exception as e:
        print(f"\n❌ CRITICAL ERROR: {e}")


if __name__ == "__main__":
    main()
