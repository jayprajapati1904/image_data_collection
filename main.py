import requests
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.http import MediaIoBaseUpload
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
import datetime
import io
import os
import time
from dotenv import load_dotenv

# =========================
# LOAD ENV
# =========================
load_dotenv()

# =========================
# CONFIG
# =========================
IMAGES_PER_DAY = 5
SCOPES = ['https://www.googleapis.com/auth/drive']


# =========================
# LOAD ANIMALS FROM GIST
# =========================
def load_animals_from_gist():
    url = "https://gist.githubusercontent.com/EyeOfMidas/311e77b8b8c2f334fc8bdaf652c1f47f/raw"

    response = requests.get(url, timeout=20)
    response.raise_for_status()

    animals = []
    seen = set()

    for line in response.text.split("\n"):
        if not line.strip():
            continue

        # Remove number prefix like "102,dog"
        clean_name = line.split(",", 1)[-1].strip().lower()

        if clean_name and clean_name not in seen:
            animals.append(clean_name)
            seen.add(clean_name)

    print(f"✅ Loaded {len(animals)} cleaned unique animals")
    return animals


# =========================
# GOOGLE DRIVE AUTH
# =========================
def authenticate_drive():
    creds = None

    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("🔄 Refreshing expired token...")
            creds.refresh(Request())
        else:
            raise ValueError("❌ 'token.json' missing or invalid! Run setup_token.py first.")

    return build('drive', 'v3', credentials=creds)


# =========================
# MONGODB
# =========================
def get_mongo_collection():
    mongo_uri = os.environ.get("MONGO_URI")
    if not mongo_uri:
        raise ValueError("❌ MONGO_URI missing")

    client = MongoClient(mongo_uri)

    try:
        client.admin.command('ping')
    except Exception as e:
        raise ConnectionError(f"❌ MongoDB Error: {e}")

    db = client["PetProject_DB"]
    collection = db["images_metadata"]

    # Unique on pixabay_id
    collection.create_index("pixabay_id", unique=True)

    return collection


# =========================
# GET OR CREATE SUBFOLDER
# =========================
def get_or_create_subfolder(service, folder_name, parent_id):
    page_token = None

    while True:
        results = service.files().list(
            q=f"'{parent_id}' in parents and trashed=false and mimeType='application/vnd.google-apps.folder'",
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
            pageSize=1000
        ).execute()

        files = results.get('files', [])

        for file in files:
            if file['name'].strip().lower() == folder_name.strip().lower():
                return file['id']

        page_token = results.get('nextPageToken')
        if not page_token:
            break

    # Folder not found -> create it
    file_metadata = {
        'name': folder_name,
        'parents': [parent_id],
        'mimeType': 'application/vnd.google-apps.folder'
    }

    folder = service.files().create(
        body=file_metadata,
        fields='id'
    ).execute()

    print(f"📁 Created folder: {folder_name}")
    return folder.get('id')


# =========================
# CHECK FILE EXISTS IN DRIVE FOLDER
# =========================
def file_exists_in_folder(service, folder_id, filename):
    """
    Prevent duplicate uploads in Drive.
    Also handles old bad names like: 102,dog_5081.jpg
    """
    page_token = None

    while True:
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
            pageSize=1000
        ).execute()

        files = results.get("files", [])

        for file in files:
            existing_name = file["name"].strip()

            # Remove old prefix like "102,"
            if "," in existing_name:
                parts = existing_name.split(",", 1)
                if parts[0].strip().isdigit():
                    existing_name = parts[1].strip()

            if existing_name.lower() == filename.lower():
                return True

        page_token = results.get("nextPageToken")
        if not page_token:
            break

    return False


# =========================
# MAIN
# =========================
def main():
    print("\n🚀 JOB STARTED\n")

    start_time = datetime.datetime.now()
    today_str = str(datetime.date.today())

    total_uploaded = 0
    total_skipped_mongo = 0
    total_skipped_drive = 0
    total_no_results = 0
    errors_log = []

    try:
        # -------------------------
        # LOAD ANIMALS
        # -------------------------
        ANIMALS = load_animals_from_gist()

        # -------------------------
        # ENV CHECK
        # -------------------------
        api_key = os.environ.get("PIXABAY_KEY")
        drive_folder_id = os.environ.get("DRIVE_FOLDER_ID")

        if not api_key:
            raise ValueError("❌ Missing PIXABAY_KEY")
        if not drive_folder_id:
            raise ValueError("❌ Missing DRIVE_FOLDER_ID")

        # -------------------------
        # AUTH
        # -------------------------
        drive_service = authenticate_drive()
        mongo_collection = get_mongo_collection()

        # -------------------------
        # DATE LOGIC
        # -------------------------
        project_start_date = datetime.date(2025, 12, 24)
        days_passed = (datetime.date.today() - project_start_date).days
        day_number = max(days_passed + 1, 1)

        print(f"📅 Fetching Pixabay Page: {day_number}")
        print(f"🦁 Total Animals to Process: {len(ANIMALS)}\n")

        # -------------------------
        # MAIN LOOP
        # -------------------------
        for animal in ANIMALS:
            print(f"\n🐾 Checking: {animal}")

            try:
                url = (
                    f"https://pixabay.com/api/"
                    f"?key={api_key}"
                    f"&q={animal}"
                    f"&image_type=photo"
                    f"&per_page={IMAGES_PER_DAY}"
                    f"&page={day_number}"
                )

                response = requests.get(url, timeout=20)
                response.raise_for_status()

                data = response.json()
                hits = data.get("hits", [])

                # 1️⃣ No Pixabay results
                if not hits:
                    print(f"   ⚠️ No images found")
                    total_no_results += 1
                    continue

                # 2️⃣ Keep only new images (not in Mongo)
                new_hits = []
                for hit in hits:
                    pixabay_id = hit["id"]

                    if mongo_collection.find_one({"pixabay_id": pixabay_id}):
                        print(f"   ⏩ Already in Mongo: {pixabay_id}")
                        total_skipped_mongo += 1
                        continue

                    new_hits.append(hit)

                # 3️⃣ If no new images, don't create folder
                if not new_hits:
                    print(f"   🧹 No new images to upload")
                    continue

                # 4️⃣ Only now create / reuse folder
                animal_folder_id = get_or_create_subfolder(
                    drive_service,
                    animal.title(),
                    drive_folder_id
                )

                # 5️⃣ Upload only truly new images
                uploaded_for_this_animal = 0

                for hit in new_hits:
                    pixabay_id = hit["id"]
                    safe_animal = animal.replace(" ", "_").lower()
                    filename = f"{safe_animal}_{pixabay_id}.jpg"

                    # Drive duplicate check too
                    if file_exists_in_folder(drive_service, animal_folder_id, filename):
                        print(f"   ⏩ Already in Drive: {filename}")
                        total_skipped_drive += 1
                        continue

                    print(f"   ⬇️ Uploading: {filename}")

                    img_response = requests.get(hit["webformatURL"], timeout=30)
                    img_response.raise_for_status()
                    img_content = img_response.content

                    fh = io.BytesIO(img_content)
                    media = MediaIoBaseUpload(
                        fh,
                        mimetype="image/jpeg",
                        resumable=True
                    )

                    drive_file = (
                        drive_service.files()
                        .create(
                            body={
                                "name": filename,
                                "parents": [animal_folder_id]
                            },
                            media_body=media,
                            fields="id, webViewLink"
                        )
                        .execute()
                    )

                    document = {
                        "name": animal,
                        "pixabay_id": pixabay_id,
                        "animal_type": animal,
                        "tags": hit.get("tags", ""),
                        "photographer": hit.get("user", ""),
                        "google_drive_url": drive_file.get("webViewLink"),
                        "date_added": today_str,
                        "original_source": hit.get("pageURL", "")
                    }

                    try:
                        mongo_collection.insert_one(document)
                    except DuplicateKeyError:
                        print(f"   ⚠️ Duplicate Mongo insert skipped: {pixabay_id}")

                    total_uploaded += 1
                    uploaded_for_this_animal += 1

                    # small delay to avoid rate issues
                    time.sleep(0.3)

                print(f"   ✅ Uploaded {uploaded_for_this_animal} new image(s)")

            except Exception as e:
                error_msg = f"Error with {animal}: {str(e)}"
                print(f"   ❌ {error_msg}")
                errors_log.append(error_msg)

        # -------------------------
        # FINAL SUMMARY
        # -------------------------
        end_time = datetime.datetime.now()
        duration = end_time - start_time

        print("\n" + "=" * 50)
        print("🎉 JOB COMPLETE")
        print("=" * 50)
        print(f"📅 Date: {today_str}")
        print(f"⏱️ Duration: {str(duration).split('.')[0]}")
        print(f"🖼️ Uploaded: {total_uploaded}")
        print(f"🗃️ Skipped (Mongo): {total_skipped_mongo}")
        print(f"📂 Skipped (Drive): {total_skipped_drive}")
        print(f"⚠️ No Pixabay Results: {total_no_results}")
        print(f"❌ Errors: {len(errors_log)}")

        if errors_log:
            print("\n⚠️ Error Log:")
            for err in errors_log[:20]:
                print(f" - {err}")

    except Exception as main_error:
        print(f"\n❌ CRITICAL FAILURE: {main_error}")


# =========================
# RUN
# =========================
if __name__ == '__main__':
    main()
