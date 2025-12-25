import requests
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.http import MediaIoBaseUpload
from pymongo import MongoClient
import datetime
import io
import os
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

# --- LOAD SECRETS LOCALLY ---
load_dotenv()

# --- CONFIGURATION ---
IMAGES_PER_DAY = 5
ANIMALS = [
    # Domestic Pets
    "dog", "cat", "rabbit", "hamster", "guinea pig", "ferret", "mouse", "rat", "gerbil", "chinchilla",
    "hedgehog", "sugar glider", "turtle", "tortoise", "parrot", "budgie", "cockatiel", "canary", "goldfish", "betta fish",

    # Farm Animals
    "chicken", "cow", "pig", "sheep", "goat", "horse", "donkey", "duck", "goose", "turkey", "llama", "alpaca", "buffalo", "ox",

    # Wild Mammals
    "lion", "tiger", "elephant", "giraffe", "zebra", "bear", "wolf", "fox", "deer", "moose", "kangaroo", "koala", "panda",
    "cheetah", "leopard", "jaguar", "rhinoceros", "hippopotamus", "gorilla", "chimpanzee", "monkey", "squirrel", "raccoon",
    "skunk", "bat", "otter", "beaver", "badger", "weasel", "coyote", "bobcat", "lynx", "walrus", "seal", "whale", "dolphin",

    # Birds
    "eagle", "hawk", "owl", "sparrow", "robin", "cardinal", "blue jay", "crow", "raven", "pigeon", "dove", "peacock",
    "flamingo", "penguin", "ostrich", "emu", "hummingbird", "woodpecker", "seagull", "swan",

    # Reptiles & Amphibians
    "snake", "lizard", "crocodile", "alligator", "frog", "toad", "salamander", "iguana", "chameleon", "gecko",

    # Aquatic Animals
    "shark", "octopus", "squid", "jellyfish", "starfish", "seahorse", "crab", "lobster", "shrimp", "clownfish",

    # Insects & Arachnids
    "butterfly", "bee", "ant", "spider", "ladybug", "grasshopper", "cricket", "dragonfly", "moth", "beetle"
]  # Add your full list back here

# --- EMAIL STYLES (CSS) ---
# We use this CSS to make the emails look professional and clean.
HTML_STYLE = """
<style>
    body { font-family: 'Helvetica', 'Arial', sans-serif; background-color: #f4f4f4; padding: 20px; }
    .container { max-width: 600px; margin: 0 auto; background: #ffffff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
    .header { text-align: center; padding-bottom: 20px; border-bottom: 1px solid #eeeeee; }
    .header h1 { margin: 0; font-size: 24px; }
    .content { padding: 20px 0; color: #333333; line-height: 1.6; }
    .stats-table { width: 100%; border-collapse: collapse; margin: 20px 0; }
    .stats-table td { padding: 10px; border-bottom: 1px solid #eeeeee; }
    .stats-table td:first-child { font-weight: bold; color: #555555; width: 40%; }
    .footer { text-align: center; font-size: 12px; color: #999999; margin-top: 30px; }
    .status-badge { display: inline-block; padding: 5px 10px; border-radius: 4px; color: white; font-weight: bold; font-size: 14px; }
    .bg-blue { background-color: #3498db; }
    .bg-green { background-color: #2ecc71; }
    .bg-red { background-color: #e74c3c; }
</style>
"""


def send_html_email(subject, html_content):
    """Sends a beautifully formatted HTML email."""
    sender_email = os.environ.get("EMAIL_SENDER")
    sender_password = os.environ.get("EMAIL_PASSWORD")
    receiver_email = os.environ.get("EMAIL_RECEIVER")

    if not sender_email or not sender_password:
        print("⚠️ Email secrets missing. Skipping email.")
        return

    try:
        msg = MIMEMultipart()
        msg['From'] = f"Daily Zoo Bot <{sender_email}>"
        msg['To'] = receiver_email
        msg['Subject'] = subject

        # Attach HTML Body
        full_html = f"""
        <html>
        <head>{HTML_STYLE}</head>
        <body>
            <div class="container">
                {html_content}
                <div class="footer">
                    Sent automatically by your Daily Zoo Python Script 🐍
                </div>
            </div>
        </body>
        </html>
        """
        msg.attach(MIMEText(full_html, 'html'))

        # Connect to Gmail
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, receiver_email, msg.as_string())
        server.quit()
        print(f"📧 HTML Email sent: {subject}")
    except Exception as e:
        print(f"❌ Failed to send email: {e}")


def authenticate_drive():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file(
            'token.json', ['https://www.googleapis.com/auth/drive'])

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("🔄 Refreshing expired token...")
            creds.refresh(Request())
        else:
            raise ValueError("'token.json' missing! Run setup_token.py first.")
    return build('drive', 'v3', credentials=creds)


def get_mongo_collection():
    mongo_uri = os.environ.get("MONGO_URI")
    if not mongo_uri:
        raise ValueError("MONGO_URI missing")

    client = MongoClient(mongo_uri)
    try:
        client.admin.command('ping')
    except Exception as e:
        raise ConnectionError(f"MongoDB Error: {e}")

    db = client["PetProject_DB"]
    collection = db["images_metadata"]
    collection.create_index("pixabay_id", unique=True)
    return collection


def get_or_create_subfolder(service, folder_name, parent_id):
    query = f"name='{folder_name}' and '{parent_id}' in parents and trashed=false"
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get('files', [])
    if files:
        return files[0]['id']

    file_metadata = {
        'name': folder_name, 'parents': [parent_id],
        'mimeType': 'application/vnd.google-apps.folder'
    }
    folder = service.files().create(body=file_metadata, fields='id').execute()
    return folder.get('id')


def main():
    start_time = datetime.datetime.now()
    today_str = str(datetime.date.today())

    # 1. SEND START EMAIL (Beautiful Blue Theme)
    print("--- STARTING JOB ---")
    start_html = f"""
    <div class="header">
        <h1 style="color: #3498db;">🚀 Daily Zoo Job Started</h1>
    </div>
    <div class="content">
        <p>The automation script has officially started running.</p>
        <table class="stats-table">
            <tr><td>📅 Date</td><td>{today_str}</td></tr>
            <tr><td>⏰ Start Time</td><td>{start_time.strftime('%H:%M:%S')}</td></tr>
            <tr><td>🦁 Animals to Check</td><td>{len(ANIMALS)}</td></tr>
        </table>
        <p>You will receive another email when the job is complete.</p>
    </div>
    """
    send_html_email(f"🚀 Job Started: {today_str}", start_html)

    total_uploaded = 0
    errors_log = []

    try:
        # --- SETUP & AUTH ---
        api_key = os.environ.get("PIXABAY_KEY")
        drive_folder_id = os.environ.get("DRIVE_FOLDER_ID")
        if not api_key:
            raise ValueError("Missing PIXABAY_KEY")
        if not drive_folder_id:
            raise ValueError("Missing DRIVE_FOLDER_ID")

        drive_service = authenticate_drive()
        mongo_collection = get_mongo_collection()

        # --- DATE LOGIC ---
        project_start_date = datetime.date(2025, 12, 24)
        days_passed = (datetime.date.today() - project_start_date).days
        day_number = days_passed + 1
        if day_number < 1:
            day_number = 1

        print(f"📅 Fetching Page {day_number}...")

        # --- MAIN LOOP ---
        for animal in ANIMALS:
            print(f"\n🐶 Checking: {animal}...")
            try:
                animal_folder_id = get_or_create_subfolder(
                    drive_service, animal.capitalize(), drive_folder_id)

                url = f"https://pixabay.com/api/?key={api_key}&q={animal}&image_type=photo&per_page={IMAGES_PER_DAY}&page={day_number}"
                response = requests.get(url)
                hits = response.json().get('hits', [])

                if not hits:
                    print(f"⚠️ No images for {animal}")
                    continue

                for hit in hits:
                    pixabay_id = hit['id']

                    # Duplicate Check
                    if mongo_collection.find_one({"pixabay_id": pixabay_id}):
                        print(f"   ⏩ Exists: {pixabay_id}")
                        continue

                    # Download & Upload
                    print(f"   ⬇️ Downloading: {pixabay_id}...")
                    img_content = requests.get(hit['webformatURL']).content
                    filename = f"{animal}_{pixabay_id}.jpg"

                    fh = io.BytesIO(img_content)
                    media = MediaIoBaseUpload(
                        fh, mimetype='image/jpeg', resumable=True)
                    drive_file = drive_service.files().create(
                        body={'name': filename, 'parents': [animal_folder_id]},
                        media_body=media, fields='id, webViewLink'
                    ).execute()

                    # Save DB
                    document = {
                        "name": animal,
                        "pixabay_id": pixabay_id,
                        "animal_type": animal,
                        "tags": hit['tags'],
                        "photographer": hit['user'],
                        "google_drive_url": drive_file.get('webViewLink'),
                        "date_added": today_str,
                        "original_source": hit['pageURL']
                    }
                    mongo_collection.insert_one(document)
                    total_uploaded += 1
                    time.sleep(1)

            except Exception as e:
                error_msg = f"Error with {animal}: {str(e)}"
                print(f"❌ {error_msg}")
                errors_log.append(error_msg)

        # 2. SEND SUCCESS EMAIL (Beautiful Green Theme)
        end_time = datetime.datetime.now()
        duration = end_time - start_time

        error_section = ""
        if errors_log:
            error_list_html = "".join(
                [f"<li style='color:red;'>{err}</li>" for err in errors_log])
            error_section = f"""
            <div style="margin-top: 20px; padding: 15px; background-color: #fff0f0; border-left: 4px solid #e74c3c;">
                <h3 style="color: #c0392b; margin-top: 0;">⚠️ Warnings/Errors</h3>
                <ul>{error_list_html}</ul>
            </div>
            """

        success_html = f"""
        <div class="header">
            <h1 style="color: #2ecc71;">✅ Job Completed Successfully</h1>
        </div>
        <div class="content">
            <p>The daily archive job has finished without critical errors.</p>
            <table class="stats-table">
                <tr><td>📅 Date</td><td>{today_str}</td></tr>
                <tr><td>⏱️ Duration</td><td>{str(duration).split('.')[0]}</td></tr>
                <tr><td>🖼️ New Images Saved</td><td><span class="status-badge bg-green">{total_uploaded}</span></td></tr>
                <tr><td>📂 Storage</td><td>Google Drive & MongoDB</td></tr>
            </table>
            {error_section}
            <p style="text-align: center; margin-top: 20px;">
                <a href="https://drive.google.com/drive/u/0/folders/{os.environ.get('DRIVE_FOLDER_ID')}" 
                   style="background-color: #3498db; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">
                   View Google Drive Folder
                </a>
            </p>
        </div>
        """
        send_html_email(
            f"✅ Job Success: {total_uploaded} New Images", success_html)
        print("\n🎉 JOB COMPLETE!")

    except Exception as main_error:
        # 3. SEND FAILURE EMAIL (Beautiful Red Theme)
        print(f"\n❌ CRITICAL FAILURE: {main_error}")

        error_html = f"""
        <div class="header">
            <h1 style="color: #e74c3c;">🚨 Job Failed</h1>
        </div>
        <div class="content">
            <p>The script crashed unexpectedly. Immediate attention required.</p>
            <div style="background-color: #ffe6e6; padding: 15px; border-radius: 5px; border: 1px solid #ffcccc;">
                <strong>Error Message:</strong><br>
                <code>{str(main_error)}</code>
            </div>
            <table class="stats-table">
                <tr><td>📅 Date</td><td>{today_str}</td></tr>
                <tr><td>❌ Status</td><td><span class="status-badge bg-red">CRASHED</span></td></tr>
            </table>
        </div>
        """
        send_html_email("🚨 Job Failed: Critical Error", error_html)


if __name__ == '__main__':
    main()
