import streamlit as st
import pandas as pd
import os
import re
import json
import datetime
from zoneinfo import ZoneInfo
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

# --- TIMEZONE CONFIGURATION ---
ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")

# --- GOOGLE RESOURCE IDs ---
DRIVE_FOLDER_ID = "1mlGrUzpwMxWRhLcXCEl9Y9u-DLeqnr6k"
SHEET_ID = "1F4YZZ9h3BLWplZFCKWE0X7yFldcXSnw38Bri_zUtb6QE"

# --- DOMAIN SECURITY & ADMIN CONFIGURATION ---
ALLOWED_DOMAIN = "@istek.k12.tr"
ADMIN_EMAILS = ["serant.senyaylar@istek.k12.tr"]

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

# --- LIGHT & DARK MODE COMPATIBLE STYLING ---
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

.stApp h1, .stApp h2, .stApp h3 {
    font-weight: 700 !important;
    letter-spacing: -0.02em !important;
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

[data-testid="stIcon"], i, [class*="Material"] {
    font-family: 'Material Symbols Rounded', 'Material Icons', sans-serif !important;
}

/* Live Clock Badge Styling */
.live-clock-badge {
    font-family: 'Inter', sans-serif;
    font-size: 0.9rem;
    font-weight: 500;
    opacity: 0.85;
    margin-top: 4px;
}
</style>
""", unsafe_allow_html=True)

# Initialize Session Quota Counter
if "graded_count" not in st.session_state:
    st.session_state.graded_count = 0

# --- USER IDENTITY EXTRACTION ---
def extract_user_identity():
    user_email = ""
    user_name = ""
    
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

# --- UI HEADER & LIVE ISTANBUL CLOCK ---
col_logo, col_title = st.columns([1, 4], vertical_alignment="center")
with col_logo:
    try:
        st.image("kurum_genel_logo_2_eng.png", use_container_width=True)
    except Exception:
        pass 

with col_title:
    st.title("Mark My Words")
    st.markdown("### **İSTEK Schools Automated English Grader**")
    
    # Real-time ticking JavaScript clock anchored to Europe/Istanbul timezone
    st.components.v1.html("""
    <div id="clock" style="font-family: 'Inter', system-ui, sans-serif; font-size: 0.9rem; font-weight: 600; color: #707070;">
      📍 Loading Istanbul Time...
    </div>
    <script>
    function updateIstanbulClock() {
        const options = { 
            timeZone: 'Europe/Istanbul', 
            year: 'numeric', 
            month: 'long', 
            day: 'numeric', 
            hour: '2-digit', 
            minute: '2-digit', 
            second: '2-digit', 
            hour12: false 
        };
        const formatter = new Intl.DateTimeFormat('en-US', options);
        document.getElementById('clock').innerHTML = '🇹🇷 <b>Istanbul Local Time:</b> ' + formatter.format(new Date());
    }
    setInterval(updateIstanbulClock, 1000);
    updateIstanbulClock();
    </script>
    """, height=35)

st.markdown("---")

# --- AUTHENTICATION & ROLE MANAGEMENT ---
def check_authentication():
    is_logged_in = False
    try:
        is_logged_in = getattr(st.user, "is_logged_in", False)
    except Exception:
        is_logged_in = False

    if not is_logged_in:
        st.warning("🔒 **Restricted Access:** Teacher Portal Only")
        st.markdown(f"Please log in with your **{ALLOWED_DOMAIN}** email to access the grading portal.")
        if st.button("Log in with Google", type="primary", use_container_width=True):
            st.login("google")
        st.stop()

    user_email, user_name = extract_user_identity()

    if not user_email.endswith(ALLOWED_DOMAIN):
        st.error(f"🚫 **Access Denied:** The account **{user_email}** is not authorized.")
        st.markdown(f"You must sign in using your official **{ALLOWED_DOMAIN}** address.")
        if st.button("Sign out and try another account", type="primary", use_container_width=True):
            st.logout()
        st.stop()

    is_admin = user_email in ADMIN_EMAILS
    now_ist = datetime.datetime.now(ISTANBUL_TZ)

    with st.sidebar:
        st.markdown(f"### 👤 **User Profile**")
        st.markdown(f"**Name:** {user_name}")
        st.markdown(f"**Email:** `{user_email}`")
        st.caption(f"🕒 **Session Start (IST):** {now_ist.strftime('%H:%M:%S')}")
        st.divider()

        if is_admin:
            st.success("👑 **Admin Status: Active**")
            st.info("⚡ Batch limits & quota limits are **DISABLED**.")
            if st.button("Reset Session Quota Counter"):
                st.session_state.graded_count = 0
                st.rerun()
        else:
            st.success("✅ **Teacher Status: Active**")
            st.caption(f"Session Usage: {st.session_state.graded_count}/{MAX_PAPERS_PER_SESSION} papers")

        st.divider()
        if st.button("Log out", use_container_width=True):
            st.logout()

    return is_admin, user_email, user_name

IS_ADMIN, USER_EMAIL, USER_NAME = check_authentication()

# --- HELPER FUNCTIONS ---
def get_google_credentials():
    creds_secret = st.secrets["google_credentials"]
    if isinstance(creds_secret, str):
        creds_json = json.loads(creds_secret)
    else:
        creds_json = dict(creds_secret)
        
    scopes = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/spreadsheets']
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
        now_ist = datetime.datetime.now(ISTANBUL_TZ)
        date_stamp = now_ist.strftime("%Y-%m-%d")
        time_stamp = now_ist.strftime("%H:%M:%S")
        sheet.append_row([date_stamp, time_stamp, teacher_name, teacher_email, student, assignment, score, word_count])
    except Exception:
        pass 

def get_file_mime_type(file_name):
    ext = file_name.split('.')[-1].lower()
    mapping = {'pdf': 'application/pdf', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp'}
    return mapping.get(ext, 'application/pdf')

# --- INDIVIDUAL MODEL API EXECUTORS ---
def run_gemini(client, prompt, file_bytes, mime_type):
    try:
        document_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
        response = client.models.generate_content(
            model="gemini-3.1-pro-preview", 
            contents=[prompt, document_part]
        )
        text = response.text
        score = 0
        word_count = "N/A"
        if "DATA_ROW:" in text:
            data_line = text.split("DATA_ROW:")[-1].strip()
            parts = data_line.split("|")
            if len(parts) >= 2:
                match = re.search(r'\d+(\.\d+)?', parts[0].strip())
                if match: score = float(match.group())
                word_count = parts[1].strip()
        return score, text, word_count
    except Exception as e:
        return 0, f"Gemini Error: {str(e)}", "N/A"

def run_gpt(client, prompt, file_bytes, mime_type, file_name):
    try:
        uploaded_file = client.files.create(
            file=(file_name, file_bytes, mime_type),
            purpose="user_data"
        )
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "file", "file": {"file_id": uploaded_file.id}}
                    ]
                }]
            )
            text = response.choices[0].message.content
        finally:
            client.files.delete(uploaded_file.id)

        score = 0
        if "DATA_ROW:" in text:
            data_line = text.split("DATA_ROW:")[-1].strip()
            parts = data_line.split("|")
            if len(parts) >= 1:
                match = re.search(r'\d+(\.\d+)?', parts[0].strip())
                if match: score = float(match.group())
        return score, text
    except Exception as e:
        return 0, f"GPT-4o Error: {str(e)}"

def run_claude(client, prompt, file_bytes, mime_type):
    try:
        base64_data = base64.b64encode(file_bytes).decode("utf-8")
        media_type = "application/pdf" if mime_type == "application/pdf" else mime_type
        doc_type = "document" if mime_type == "application/pdf" else "image"
        
        media_block = {
            "type": doc_type,
            "source": {"type": "base64", "media_type": media_type, "data": base64_data}
        }

        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=2000,
            messages=[{"role": "user", "content": [media_block, {"type": "text", "text": prompt}]}]
        )
        text = response.content[0].text
        score = 0
        if "DATA_ROW:" in text:
            data_line = text.split("DATA_ROW:")[-1].strip()
            parts = data_line.split("|")
            if len(parts) >= 1:
                match = re.search(r'\d+(\.\d+)?', parts[0].strip())
                if match: score = float(match.group())
        return score, text
    except Exception as e:
        return 0, f"Claude Error: {str(e)}"

# --- MAIN LAYOUT ---
st.subheader("1. Assignment Details & Rubric")

assignment_type = st.selectbox(
    "Assignment Type", 
    ["Guided Essay Writing (120–150 words)", "Guided Paragraph Writing (70–90 words)"]
)

rubric_source = st.radio(
    "Rubric Source", 
    ["Use Pre-installed Default", "Upload Custom Rubric"], 
    horizontal=True
)

custom_rubric_file = None
if rubric_source == "Upload Custom Rubric":
    custom_rubric_file = st.file_uploader("Upload your Custom CSV Rubric", type=["csv"])
    if custom_rubric_file:
        st.success("Custom rubric loaded!")
else:
    st.info("Using official İSTEK CEFR default rubric.")

st.markdown("<br>", unsafe_allow_html=True)
st.subheader("2. Upload Student Papers")
uploaded_files = st.file_uploader(
    f"Upload Student Work (PDF, JPG, PNG)", 
    type=["pdf", "png", "jpg", "jpeg", "webp"], 
    accept_multiple_files=True
)

if IS_ADMIN:
    with st.expander("👑 Admin Tools & Live Grade Logs"):
        if st.button("Fetch Google Sheet Grade Logs"):
            try:
                sheet = get_google_sheet()
                records = sheet.get_all_records()
                st.dataframe(pd.DataFrame(records), use_container_width=True)
            except Exception as ex:
                st.error(f"Could not load sheets: {str(ex)}")

if st.button("Evaluate Papers", type="primary", use_container_width=True):
    if not uploaded_files:
        st.error("Please select at least one file to grade.")
        st.stop()
        
    if not IS_ADMIN:
        if len(uploaded_files) > MAX_FILES_PER_BATCH:
            st.error(f"⚠️ **Batch Limit Exceeded:** Teachers can upload a maximum of {MAX_FILES_PER_BATCH} papers per batch.")
            st.stop()

        if st.session_state.graded_count + len(uploaded_files) > MAX_PAPERS_PER_SESSION:
            st.error(f"🛑 **Session Limit Reached:** Quota of {MAX_PAPERS_PER_SESSION} evaluations reached for this session.")
            st.stop()

    if rubric_source == "Upload Custom Rubric" and custom_rubric_file is not None:
        rubric_text = pd.read_csv(custom_rubric_file).to_string()
    else:
        filename = "Rubric_GUIDED_ESSAY_WRITING_B1.csv" if "Essay" in assignment_type else "Rubric_GUIDED_PARAGRAPH_WRITING_B1.csv"
        if os.path.exists(filename):
            rubric_text = pd.read_csv(filename).to_string()
        else:
            st.error(f"Missing default rubric: {filename}.")
            st.stop()

    gemini_client = genai.Client(api_key=st.secrets["gemini_api_key"])
    openai_client = OpenAI(api_key=st.secrets["openai_api_key"])
    anthropic_client = anthropic.Anthropic(api_key=st.secrets["anthropic_api_key"])

    for file in uploaded_files:
        student_identifier = os.path.splitext(file.name)[0]
        file_bytes = file.getvalue()
        mime_type = get_file_mime_type(file.name)
        
        upload_file_to_drive(file_bytes, file.name, DRIVE_FOLDER_ID, mime_type)
        
        with st.spinner(f"🚀 Running Parallel Tri-Model Consensus on {file.name}..."):
            prompt = f"""
You are a veteran high school English teacher and a rigorous CEFR B1+ examiner.

**VALIDATION GUARDRAIL:**
First, check if this image/PDF contains handwritten or typed English student text.
If the document is a selfie, meme, blank page, non-academic graphic, or non-English text, STOP IMMEDIATELY and reply ONLY with:
"REJECTED: Invalid Submission. Uploaded file does not contain a valid English student essay."

Assignment Type: {assignment_type}
Rubric:
{rubric_text}

Structure your output EXACTLY like this if valid:
### 📜 Transcribed Text
(Accurately transcribe the handwriting here)

### 📝 Red Pen Corrections
(Rewrite the text, bolding errors and putting the correction in brackets next to it.)

### 📊 Word Count & Rule Compliance
(Exact word count)

### 🏆 Score Breakdown
* **Task Achievement:** [Score]
* **Organization & Style:** [Score]
* **Accuracy:** [Score]
**Total Score:** [Total]

### 💬 Teacher's Feedback
(2-3 supportive but direct sentences explaining the grade)

IMPORTANT: Output final data row as absolute last line:
DATA_ROW: [TOTAL_SCORE] | [WORD_COUNT]
"""

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future_gemini = executor.submit(run_gemini, gemini_client, prompt, file_bytes, mime_type)
                future_gpt = executor.submit(run_gpt, openai_client, prompt, file_bytes, mime_type, file.name)
                future_claude = executor.submit(run_claude, anthropic_client, prompt, file_bytes, mime_type)

                gemini_score, gemini_text, word_count = future_gemini.result()
                gpt_score, gpt_text = future_gpt.result()
                claude_score, claude_text = future_claude.result()

            if "REJECTED:" in gemini_text and "REJECTED:" in gpt_text:
                st.warning(f"⚠️ **File Skipped ({file.name}):** The AI flagged this file as unreadable or not a valid student paper.")
                continue

            st.session_state.graded_count += 1

            scores = [gemini_score, gpt_score, claude_score]
            score_diff = max(scores) - min(scores)
            final_score = round(sum(scores) / 3, 1)

            save_grade(USER_NAME, USER_EMAIL, student_identifier, assignment_type, final_score, word_count)

            now_ist_str = datetime.datetime.now(ISTANBUL_TZ).strftime('%Y-%m-%d at %H:%M:%S (TRT)')

            with st.expander(f"✅ Graded: {student_identifier} | Final Score: {final_score}", expanded=True):
                st.caption(f"Evaluated by: **{USER_NAME}** (`{USER_EMAIL}`) on {now_ist_str}")
                
                if score_diff >= 10:
                    st.warning(f"⚠️ **High Discrepancy Alert:** Models differed by {score_diff} pts. Manual review advised.")
                else:
                    st.success(f"Models in agreement (Max Difference: {score_diff} pts).")

                st.markdown(f"**Gemini 3.1 Pro:** {gemini_score} | **GPT-4o:** {gpt_score} | **Claude 3.5 Sonnet:** {claude_score}")
                st.divider()

                col1, col2, col3 = st.tabs(["🤖 Gemini", "🧠 GPT-4o", "🦉 Claude 3.5"])
                with col1:
                    st.markdown(gemini_text.split("DATA_ROW:")[0].strip())
                with col2:
                    st.markdown(gpt_text.split("DATA_ROW:")[0].strip())
                with col3:
                    st.markdown(claude_text.split("DATA_ROW:")[0].strip())
