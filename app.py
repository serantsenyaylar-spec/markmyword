import streamlit as st
import pandas as pd
import os
import json
import datetime
import base64
import concurrent.futures
from io import BytesIO

# API Integrations
from google import genai
from google.genai import types
from openai import OpenAI
import anthropic
import gspread
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# --- SECURE CONFIGURATION (FETCHED FROM ST.SECRETS) ---
DRIVE_FOLDER_ID = st.secrets.get("DRIVE_FOLDER_ID", "1mlGrUzpwMxWRhLcXCEl9Y9u-DLeqnr6k")
SHEET_ID = st.secrets.get("SHEET_ID", "1F4YZZ9h3BLWplZFCKWE0X7yFldcXSnw38Bri_zUtb6QE")
ADMIN_EMAILS = st.secrets.get("ADMIN_EMAILS", ["serant.senyaylar@istek.k12.tr"])
ALLOWED_DOMAIN = "@istek.k12.tr"

# Standard Teacher Restrictions
MAX_FILES_PER_BATCH = 5
MAX_PAPERS_PER_SESSION = 15

# --- PAGE SETTINGS ---
st.set_page_config(
    page_title="Mark My Words | İSTEK", 
    page_icon="📝", 
    layout="centered", 
    initial_sidebar_state="collapsed"
)

# --- LIGHT & DARK MODE STYLING ---
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

html, body, .stApp {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif !important;
}

.stApp p, .stApp label, .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6,
.stApp input, .stApp textarea, .stApp button, .stApp select {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif !important;
    color: var(--text-color) !important;
}

div[data-testid="stMarkdownContainer"], 
div[data-testid="stMarkdownContainer"] p,
div[data-testid="stText"], 
.stApp p {
    overflow-wrap: break-word !important;
    word-break: break-word !important;
    white-space: normal !important;
}

div[data-testid="stExpander"] {
    border: 1px solid var(--secondary-background-color) !important;
    border-radius: 10px !important;
    background-color: var(--background-color) !important;
}

div[data-testid="stButton"] > button {
    border-radius: 8px !important;
    font-weight: 600 !important;
    font-size: 0.95rem !important;
}
</style>
""", unsafe_allow_html=True)

if "graded_count" not in st.session_state:
    st.session_state.graded_count = 0

# --- USER IDENTITY EXTRACTION ---
def extract_user_identity():
    user_email, user_name = "", ""
    try:
        user_email = getattr(st.user, "email", "") or st.user.get("email", "")
        user_name = getattr(st.user, "name", "") or st.user.get("name", "")
    except Exception:
        pass

    if user_email and not user_name:
        name_part = user_email.split("@")[0]
        tokens = name_part.split(".")
        user_name = " ".join([t.capitalize() for t in tokens])

    return user_email, user_name or "Teacher User"

# --- UI HEADER & DYNAMIC CLOCK ---
col_logo, col_title = st.columns([1, 4], vertical_alignment="center")
with col_logo:
    try:
        st.image("kurum_genel_logo_2_eng.png", use_container_width=True)
    except Exception:
        pass 

with col_title:
    st.title("Mark My Words")
    st.markdown("### **İSTEK Schools Automated English Grader**")
    
    st.components.v1.html("""
    <div id="clock" style="font-family: 'Inter', system-ui, sans-serif; font-size: 0.9rem; font-weight: 600; color: #707070;">
      🌐 Detecting local timezone...
    </div>
    <script>
    function updateAdaptiveClock() {
        const userTZ = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
        const options = { 
            timeZone: userTZ, year: 'numeric', month: 'long', day: 'numeric', 
            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false 
        };
        const formatter = new Intl.DateTimeFormat('en-US', options);
        document.getElementById('clock').innerHTML = '🌐 <b>Local Time (' + userTZ + '):</b> ' + formatter.format(new Date());
    }
    setInterval(updateAdaptiveClock, 1000);
    updateAdaptiveClock();
    </script>
    """, height=35)

st.markdown("---")

# --- AUTHENTICATION & ROLE MANAGEMENT ---
def check_authentication():
    is_logged_in = getattr(st.user, "is_logged_in", False) if hasattr(st, "user") else False

    if not is_logged_in:
        st.warning("🔒 **Restricted Access:** Teacher Portal Only")
        st.markdown(f"Please log in with your **{ALLOWED_DOMAIN}** email to access the portal.")
        if st.button("Log in with Google", type="primary", use_container_width=True):
            st.login("google")
        st.stop()

    user_email, user_name = extract_user_identity()

    if not user_email.endswith(ALLOWED_DOMAIN):
        st.error(f"🚫 **Access Denied:** The account **{user_email}** is not authorized.")
        if st.button("Sign out and try another account", type="primary", use_container_width=True):
            st.logout()
        st.stop()

    is_admin = user_email in ADMIN_EMAILS

    with st.sidebar:
        st.markdown(f"### 👤 **User Profile**")
        st.markdown(f"**Name:** {user_name}\n**Email:** `{user_email}`")
        st.divider()

        if is_admin:
            st.success("👑 **Admin Status: Active**")
            st.info("⚡ Quotas & batch limits disabled.")
            if st.button("Reset Session Quota Counter"):
                st.session_state.graded_count = 0
                st.rerun()
        else:
            st.success("✅ **Teacher Status: Active**")
            st.caption(f"Session Usage: {st.session_state.graded_count}/{MAX_PAPERS_PER_SESSION}")

        st.divider()
        if st.button("Log out", use_container_width=True):
            st.logout()

    return is_admin, user_email, user_name

IS_ADMIN, USER_EMAIL, USER_NAME = check_authentication()

# --- HELPER & API FUNCTIONS ---
def get_google_credentials():
    creds_secret = st.secrets["google_credentials"]
    creds_json = json.loads(creds_secret) if isinstance(creds_secret, str) else dict(creds_secret)
    # Reduced scope to drive.file for security compliance
    scopes = ['https://www.googleapis.com/auth/drive.file', 'https://www.googleapis.com/auth/spreadsheets']
    return service_account.Credentials.from_service_account_info(creds_json, scopes=scopes)

def upload_file_to_drive(file_bytes, file_name, folder_id, mime_type):
    try:
        creds = get_google_credentials()
        service = build('drive', 'v3', credentials=creds)
        file_metadata = {'name': file_name, 'parents': [folder_id]}
        media = MediaIoBaseUpload(BytesIO(file_bytes), mimetype=mime_type, resumable=True)
        service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        return True
    except Exception:
        return False

def get_google_sheet():
    creds = get_google_credentials()
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).sheet1

def save_grade(teacher_name, teacher_email, student, assignment, score, word_count):
    try:
        sheet = get_google_sheet()
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        sheet.append_row([
            now_utc.strftime("%Y-%m-%d"), 
            now_utc.strftime("%H:%M:%S UTC"), 
            teacher_name, teacher_email, student, assignment, score, word_count
        ])
    except Exception:
        pass 

def parse_json_response(raw_text):
    clean_text = raw_text.strip()
    if clean_text.startswith("```json"):
        clean_text = clean_text.split("```json")[1].split("```")[0].strip()
    elif clean_text.startswith("```"):
        clean_text = clean_text.split("
