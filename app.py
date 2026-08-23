import streamlit as st
import pandas as pd
import os
import json
import datetime
import base64
import html
import mimetypes
import zipfile
import io
import time
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

# --- CONFIGURATION ---
DRIVE_FOLDER_ID = st.secrets.get("DRIVE_FOLDER_ID", "1mlGrUzpwMxWRhLcXCEl9Y9u-DLeqnr6k")
SHEET_ID = st.secrets.get("SHEET_ID", "1F4YZZ9h3BLWplZFCKWE0X7yFldcXSnw38Bri_zUtb6QE")
ADMIN_EMAILS = st.secrets.get("ADMIN_EMAILS", ["serant.senyaylar@istek.k12.tr", "serantsenyaylar-spec"])
ALLOWED_DOMAIN = "@istek.k12.tr"

MAX_FILES_PER_BATCH = 5
MAX_PAPERS_PER_SESSION = 15

# --- PAGE SETTINGS ---
st.set_page_config(
    page_title="Mark My Words | İSTEK", 
    page_icon="📝", 
    layout="wide", 
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

.user-card {
    background-color: var(--secondary-background-color);
    padding: 12px 14px;
    border-radius: 10px;
    border: 1px solid rgba(128, 128, 128, 0.2);
    margin-bottom: 10px;
}
.user-card-name {
    font-weight: 700;
    font-size: 1.05rem;
    margin-bottom: 2px;
}
.user-card-email {
    font-size: 0.82rem;
    opacity: 0.8;
    word-break: break-all;
}
</style>
""", unsafe_allow_html=True)

# --- SESSION STATE INITIALIZATION ---
if "graded_count" not in st.session_state:
    st.session_state.graded_count = 0

if "graded_results" not in st.session_state:
    st.session_state.graded_results = []

if "auth_user" not in st.session_state:
    st.session_state.auth_user = None

if "preset_template" not in st.session_state:
    st.session_state.preset_template = "Guided Essay Writing (120–150 words)"

if "demo_loaded" not in st.session_state:
    st.session_state.demo_loaded = False

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

    if user_email:
        st.session_state.auth_user = {"email": user_email, "name": user_name or "Teacher User"}
    elif st.session_state.auth_user:
        user_email = st.session_state.auth_user.get("email", "")
        user_name = st.session_state.auth_user.get("name", "")

    return user_email, user_name or "Teacher User"

# --- UI HEADER & CLOCK ---
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
      🌐 Detecting your local timezone...
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

# --- AUTHENTICATION ---
def check_authentication():
    is_logged_in = getattr(st.user, "is_logged_in", False) if hasattr(st, "user") else False

    if not is_logged_in and not st.session_state.auth_user:
        st.warning("🔒 **Restricted Access:** Teacher Portal Only")
        st.markdown(f"Please log in with your **{ALLOWED_DOMAIN}** email to access the portal.")
        if st.button("Log in with Google", type="primary", use_container_width=True):
            st.login("google")
        st.stop()

    user_email, user_name = extract_user_identity()

    is_admin = any(
        admin.lower() in [user_email.lower(), user_name.lower()] 
        for admin in ADMIN_EMAILS
    )

    if not is_admin and not user_email.endswith(ALLOWED_DOMAIN):
        st.error(f"🚫 **Access Denied:** The account **{user_email}** is not authorized.")
        if st.button("Sign out and try another account", type="primary", use_container_width=True):
            st.session_state.auth_user = None
            st.logout()
        st.stop()

    with st.sidebar:
        safe_name = html.escape(user_name)
        safe_email = html.escape(user_email or 'Verified User')
        
        st.markdown("### 👤 **Account Details**")
        st.markdown(f"""
        <div class="user-card">
            <div class="user-card-name">👤 {safe_name}</div>
            <div class="user-card-email">📧 {safe_email}</div>
        </div>
        """, unsafe_allow_html=True)

        if is_admin:
            st.success("👑 **Admin Status: Active**")
            st.caption("⚡ Quotas & batch limits disabled.")
            if st.button("Reset Quota Counter", use_container_width=True):
                st.session_state.graded_count = 0
                st.session_state.graded_results = []
                st.rerun()
        else:
            st.success("✅ **Teacher Status: Active**")
            st.caption(f"Session Usage: {st.session_state.graded_count}/{MAX_PAPERS_PER_SESSION} papers")

        st.divider()

        st.markdown("### 🌐 **Google Workspace**")
        gcol1, gcol2 = st.columns(2)
        with gcol1:
            st.link_button("🏫 Classroom", "https://classroom.google.com", use_container_width=True)
            st.link_button("📁 Drive", "https://drive.google.com", use_container_width=True)
            st.link_button("📄 Docs", "https://docs.google.com", use_container_width=True)
        with gcol2:
            st.link_button("📧 Gmail", "https://mail.google.com", use_container_width=True)
            st.link_button("📊 Sheets", "https://sheets.google.com", use_container_width=True)
            st.link_button("📅 Calendar", "https://calendar.google.com", use_container_width=True)

        st.divider()
        if st.button("Log out", use_container_width=True):
            st.session_state.auth_user = None
            st.logout()

    return is_admin, user_email, user_name

IS_ADMIN, USER_EMAIL, USER_NAME = check_authentication()

# --- HELPER & GOOGLE API FUNCTIONS ---
def get_google_credentials():
    if "google_credentials" not in st.secrets:
        return None
    creds_secret = st.secrets["google_credentials"]
    creds_json = json.loads(creds_secret) if isinstance(creds_secret, str) else dict(creds_secret)
    scopes = ['https://www.googleapis.com/auth/drive.file', 'https://www.googleapis.com/auth/spreadsheets']
    return service_account.Credentials.from_service_account_info(creds_json, scopes=scopes)

def upload_file_to_drive(file_bytes, file_name, folder_id, mime_type):
    try:
        creds = get_google_credentials()
        if not creds:
            return False
        service = build('drive', 'v3', credentials=creds)
        file_metadata = {'name': file_name, 'parents': [folder_id]}
        media = MediaIoBaseUpload(BytesIO(file_bytes), mimetype=mime_type, resumable=True)
        service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        return True
    except Exception:
        return False

def get_google_sheet():
    creds = get_google_credentials()
    if not creds:
        return None
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).sheet1

def save_grade(teacher_name, teacher_email, student, assignment, score, word_count, total_scale):
    try:
        sheet = get_google_sheet()
        if not sheet:
            return
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        sheet.append_row([
            now_utc.strftime("%Y-%m-%d"), 
            now_utc.strftime("%H:%M:%S UTC"), 
            teacher_name, teacher_email, student, assignment, f"{score}/{total_scale}", word_count
        ])
    except Exception:
        pass 

def parse_json_response(raw_text):
    clean_text = raw_text.strip()
    fence = chr(96) * 3
    if fence in clean_text:
        parts = clean_text.split(fence)
        for part in parts:
            p = part.strip()
            if p.lower().startswith("json"):
                p = p[4:].strip()
            if p.startswith("{") and p.endswith("}"):
                clean_text = p
                break
    return json.loads(clean_text)

def check_validity(res_dict):
    if not isinstance(res_dict, dict):
        return False
    val = res_dict.get("is_valid_submission", False)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ["true", "1", "yes"]
    return False

# --- STRUCTURED EVALUATION RUNNERS ---
SYSTEM_PROMPT = """You are a veteran CEFR B1+ high school English examiner.
Evaluate the student essay based STRICTLY on the provided rubric in <rubric_data> and the specific assignment prompt in <assignment_question>.

Return your evaluation EXACTLY as a JSON object matching this schema:
{
  "is_valid_submission": true,
  "rejection_reason": "N/A or detail",
  "transcribed_text": "...",
  "red_pen_corrections": "...",
  "word_count": 0,
  "score_task_achievement": 0,
  "score_organization": 0,
  "score_accuracy": 0,
  "total_score": 0,
  "feedback": "..."
}"""

def run_gemini_structured(client, user_prompt, file_bytes, mime_type):
    candidate_models = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-flash", "gemini-2.0-flash"]
    last_err = ""

    for model_name in candidate_models:
        for attempt in range(4):
            try:
                if mime_type.startswith("text/"):
                    text_str = file_bytes.decode("utf-8", errors="ignore")
                    contents = [SYSTEM_PROMPT, f"{user_prompt}\n\nStudent Essay File:\n{text_str}"]
                else:
                    doc_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
                    contents = [SYSTEM_PROMPT, user_prompt, doc_part]

                response = client.models.generate_content(
                    model=model_name, 
                    contents=contents,
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
                res_dict = json.loads(response.text)
                res_dict["model_used"] = model_name
                return res_dict
            except Exception as e:
                err_str = str(e)
                last_err = f"{model_name}: {err_str}"
                
                if any(code in err_str for code in ["503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED"]):
                    wait_time = 2 ** (attempt + 1)
                    time.sleep(wait_time)
                    continue
                else:
                    break

    return {
        "is_valid_submission": False, 
        "rejection_reason": f"Gemini Capacity Limit: {last_err}", 
        "total_score": 0, 
        "word_count": 0
    }

def run_gpt_structured(client, user_prompt, file_bytes, mime_type, file_name):
    try:
        if mime_type.startswith("text/"):
            text_str = file_bytes.decode("utf-8", errors="ignore")
            content_payload = f"{user_prompt}\n\nStudent Essay File:\n{text_str}"
        elif mime_type.startswith("image/"):
            b64 = base64.b64encode(file_bytes).decode("utf-8")
            content_payload = [
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}}
            ]
        else:
            content_payload = f"{user_prompt}\n\nDocument File Name: {file_name}"

        res = client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content_payload}
            ]
        )
        return json.loads(res.choices[0].message.content)
    except Exception as e:
        return {"is_valid_submission": False, "rejection_reason": f"GPT Error: {str(e)}", "total_score": 0, "word_count": 0}

def run_claude_structured(client, user_prompt, file_bytes, mime_type):
    try:
        if mime_type.startswith("text/"):
            text_str = file_bytes.decode("utf-8", errors="ignore")
            content_payload = [
                {"type": "text", "text": f"{user_prompt}\n\nStudent Essay File:\n{text_str}\nReturn strictly JSON."}
            ]
        elif mime_type == "application/pdf":
            b64 = base64.b64encode(file_bytes).decode("utf-8")
            content_payload = [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
                {"type": "text", "text": user_prompt + "\nReturn strictly JSON."}
            ]
        else:
            b64 = base64.b64encode(file_bytes).decode("utf-8")
            content_payload = [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                {"type": "text", "text": user_prompt + "\nReturn strictly JSON."}
            ]

        res = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content_payload}]
        )
        return parse_json_response(res.content[0].text)
    except Exception as e:
        return {"is_valid_submission": False, "rejection_reason": f"Claude Error: {str(e)}", "total_score": 0, "word_count": 0}

# --- TEACHER DASHBOARD ---
st.markdown(f"### 👋 Welcome back, **{USER_NAME}**")
hm1, hm2, hm3 = st.columns(3)
hm1.metric("Papers Graded (Session)", f"{st.session_state.graded_count} / {MAX_PAPERS_PER_SESSION}")
hm2.metric("Batch Limit per Run", "Unlimited" if IS_ADMIN else f"{MAX_FILES_PER_BATCH} Files")
hm3.metric("Account Role", "👑 Admin User" if IS_ADMIN else "✅ Teacher User")

st.markdown("<br>", unsafe_allow_html=True)

# --- 3-STEP WIZARD TABS ---
wizard_tab1, wizard_tab2, wizard_tab3 = st.tabs([
    "⚙️ Step 1: Prompt & Rubric Setup", 
    "📤 Step 2: Upload & File Pre-Flight", 
    "📊 Step 3: Evaluation & Reports"
])

# --- TAB 1: SETUP ---
with wizard_tab1:
    st.markdown("#### ⚡ Quick-Start Templates")
    qc1, qc2, qc3 = st.columns(3)
    with qc1:
        if st.button("📝 B1 Guided Essay\n(120–150 words)", use_container_width=True):
            st.session_state.preset_template = "Guided Essay Writing (120–150 words)"
            st.rerun()
    with qc2:
        if st.button("📄 B1 Guided Paragraph\n(70–90 words)", use_container_width=True):
            st.session_state.preset_template = "Guided Paragraph Writing (70–90 words)"
            st.rerun()
    with qc3:
        if st.button("🎨 Custom Assignment\n(Upload Prompt & Rubric)", use_container_width=True):
            st.session_state.preset_template = "Guided Essay Writing (120–150 words)"
            st.rerun()

    st.divider()

    col_assign1, col_assign2 = st.columns([1, 1])

    with col_assign1:
        assignment_type = st.selectbox(
            "Assignment Type", 
            ["Guided Essay Writing (120–150 words)", "Guided Paragraph Writing (70–90 words)"],
            index=0 if st.session_state.preset_template == "Guided Essay Writing (120–150 words)" else 1
        )

        question_option = st.radio("Assignment Questions / Prompt Source", ["Use Preset Question Prompt", "Type Custom Question / Prompt", "Upload Question File (.txt / .json)"], horizontal=True)

        default_essay_question = "Write a 120-150 word guided essay discussing how technology influences modern student communication. Include examples from your personal school experience."
        default_para_question = "Write a 70-90 word paragraph describing your ideal morning routine before school starts. Explain why each activity helps your day."

        if question_option == "Use Preset Question Prompt":
            active_question = default_essay_question if "Essay" in assignment_type else default_para_question
            st.info(f"📌 **Current Assignment Question:**\n\n{active_question}")
        elif question_option == "Type Custom Question / Prompt":
            active_question = st.text_area(
                "Enter Assignment Question / Prompt for AI Evaluation:", 
                value=default_essay_question if "Essay" in assignment_type else default_para_question,
                height=110
            )
        else:
            q_file = st.file_uploader("Upload Question File (.txt)", type=["txt", "json"])
            if q_file:
                active_question = q_file.getvalue().decode("utf-8", errors="ignore")
                st.success("✅ Custom assignment question loaded successfully.")
            else:
                active_question = default_essay_question if "Essay" in assignment_type else default_para_question

    with col_assign2:
        default_fn = "Rubric_GUIDED_ESSAY_WRITING_B1.csv" if "Essay" in assignment_type else "Rubric_GUIDED_PARAGRAPH_WRITING_B1.csv"
        if os.path.exists(default_fn):
            default_rubric_df = pd.read_csv(default_fn)
        else:
            default_rubric_df = pd.DataFrame({
                "Criteria": ["Task Achievement", "Organization", "Grammatical Accuracy"],
                "Max Score": [35, 35, 30],
                "Description": ["Fulfills prompt criteria", "Logical structure and paragraphs", "Correct syntax, spelling, punctuation"]
            })

        rubric_source = st.radio("Rubric Source", ["Use Pre-installed Default", "Upload Custom Rubric"], horizontal=True)

        if rubric_source == "Upload Custom Rubric":
            custom_rubric_file = st.file_uploader("Upload Custom CSV Rubric", type=["csv"])
            if custom_rubric_file:
                try:
                    active_rubric_df = pd.read_csv(custom_rubric_file)
                    st.success("✅ Custom rubric loaded successfully!")
                except Exception as e:
                    st.error(f"Error loading CSV: {str(e)}")
                    active_rubric_df = default_rubric_df
            else:
                active_rubric_df = default_rubric_df
        else:
            active_rubric_df = default_rubric_df

        st.markdown("##### 🎯 Rubric Point Scale")
        
        if "Max Score" in active_rubric_df.columns:
            try:
                auto_total = int(pd.to_numeric(active_rubric_df["Max Score"]).sum())
            except Exception:
                auto_total = 100
        else:
            auto_total = 100

        total_rubric_scale = st.number_input(
            "Total Evaluation Scale (Out Of Number)", 
            min_value=1, max_value=500, value=auto_total, step=1
        )

    raw_rubric = active_rubric_df.to_string()
    st.success(f"✅ Step 1 Configured! Rubric Scale set to **{total_rubric_scale} Points**.")

# --- TAB 2: UPLOAD & EVALUATE ---
with wizard_tab2:
    st.markdown("#### 📤 Upload Student Submissions")
    
    col_up1, col_up2 = st.columns([3, 1])
    with col_up1:
        uploaded_files = st.file_uploader("Upload Student Work (PDF, JPG, PNG, TXT)", type=["pdf", "png", "jpg", "jpeg", "webp", "txt"], accept_multiple_files=True)
    with col_up2:
        st.markdown("**Test Drive App**")
        if st.button("🧪 Load Sample Paper"):
            st.session_state.demo_loaded = True

    active_files = []
    if uploaded_files:
        st.session_state.demo_loaded = False
        active_files = uploaded_files
    elif st.session_state.demo_loaded:
        sample_filename = "Sample_Student_9999.txt"
        sample_content = """Technology has completely changed how students communicate today. In the past, students called each other on landline phones or talked in person after class. Now, apps like WhatsApp and Google Classroom allow us to exchange study notes and work on group projects instantly.

For example, when our English teacher assigned a group presentation last week, we created a group chat immediately. We shared links, edited slides together, and solved questions late in the evening. However, social media can sometimes distract us during study sessions. Overall, modern technology makes academic collaboration faster and more convenient for everyone."""
        
        sample_bytes = sample_content.encode("utf-8")
        active_files = [type('UploadedDemoFile', (object,), {
            'name': sample_filename,
            'getvalue': lambda self=None: sample_bytes
        })()]
        st.info("🧪 **Sample Student Essay Loaded!** Click **Evaluate Submissions** below.")

    if active_files:
        file_table_data = []
        for index, file_obj in enumerate(active_files, start=1):
            file_bytes = file_obj.getvalue()
            size_kb = round(len(file_bytes) / 1024, 1)
            mtype = mimetypes.guess_type(file_obj.name)[0] or "text/plain"
            file_table_data.append({
                "#": index,
                "Student ID": os.path.splitext(file_obj.name)[0],
                "File Name": file_obj.name,
                "Size": f"{size_kb} KB",
                "Format": mtype.split("/")[-1].upper(),
                "Status": "Ready for AI Analysis"
            })
        st.dataframe(pd.DataFrame(file_table_data), use_container_width=True)

    st.markdown("<br>", unsafe_allow_html=True)
    
    if st.button("🚀 Evaluate Submissions", type="primary", use_container_width=True):
        if not active_files:
            st.error("Please upload at least one student paper or click 'Load Sample Paper'.")
            st.stop()

        gemini_client = genai.Client(api_key=st.secrets["gemini_api_key"])
        openai_client = OpenAI(api_key=st.secrets["openai_api_key"])
        anthropic_client = anthropic.Anthropic(api_key=st.secrets["anthropic_api_key"])

        user_prompt = f"""Assignment Type: {assignment_type}
Total Rubric Scale: Out of {total_rubric_scale} points.

<assignment_question>
{active_question}
</assignment_question>

<rubric_data>
{raw_rubric}
</rubric_data>

Evaluate the submission. Calculate total_score based on rubric criteria out of {total_rubric_scale}."""

        st.session_state.graded_results = []

        for file in active_files:
            student_id = os.path.splitext(file.name)[0]
            file_bytes = file.getvalue()
            mime_type = mimetypes.guess_type(file.name)[0] or ("text/plain" if file.name.endswith(".txt") else "application/pdf")

            upload_file_to_drive(file_bytes, file.name, DRIVE_FOLDER_ID, mime_type)

            with st.spinner(f"🚀 Evaluating {file.name}..."):
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    f_gemini = executor.submit(run_gemini_structured, gemini_client, user_prompt, file_bytes, mime_type)
                    f_gpt = executor.submit(run_gpt_structured, openai_client, user_prompt, file_bytes, mime_type, file.name)
                    f_claude = executor.submit(run_claude_structured, anthropic_client, user_prompt, file_bytes, mime_type)

                    res_g = f_gemini.result()
                    res_o = f_gpt.result()
                    res_c = f_claude.result()

                all_responses = {"Gemini": res_g, "GPT-4o": res_o, "Claude": res_c}
                valid_results = {name: r for name, r in all_responses.items() if check_validity(r)}
                
                if not valid_results:
                    st.error(f"❌ **Evaluation Failed ({file.name}):** All AI models failed. Check network connectivity or API key configurations.")
                    with st.expander(f"🔍 Diagnostic Errors for `{file.name}`", expanded=True):
                        for model_name, res in all_responses.items():
                            st.markdown(f"**{model_name}:** `{res.get('rejection_reason', 'Unknown error')}`")
                    continue

                if len(valid_results) < 3:
                    st.warning(f"💡 **Notice ({file.name}):** Evaluation completed using active models: `{', '.join(valid_results.keys())}`. (Other models skipped due to credit/quota limits).")

                st.session_state.graded_count += 1

                valid_scores = [r.get("total_score", 0) for r in valid_results.values()]
                final_score = round(sum(valid_scores) / len(valid_scores), 1)

                primary_res = list(valid_results.values())[0]
                word_count = primary_res.get("word_count", "N/A")
                transcribed_text = primary_res.get("transcribed_text", "N/A")
                feedback = primary_res.get("feedback", "N/A")

                g_score = res_g.get("total_score", "Quota Exceeded") if check_validity(res_g) else "Skipped"
                o_score = res_o.get("total_score", "Quota Exceeded") if check_validity(res_o) else "Skipped"
                c_score = res_c.get("total_score", "Quota Exceeded") if check_validity(res_c) else "Skipped"

                scores_summary = f"Gemini: {g_score} | GPT-4o: {o_score} | Claude: {c_score}"

                save_grade(USER_NAME, USER_EMAIL, student_id, assignment_type, final_score, word_count, total_rubric_scale)

                report_text = f"""================================================================================
İSTEK SCHOOLS AUTOMATED ENGLISH GRADING REPORT
================================================================================
Student ID : {student_id} | Assignment: {assignment_type}
Evaluated By: {USER_NAME} ({USER_EMAIL})
Final Consensus Score: {final_score} / {total_rubric_scale}
Model Scores Summary: {scores_summary}
================================================================================
Target Question / Prompt:
{active_question}

Transcribed Text:
{transcribed_text}

Feedback:
{feedback}
================================================================================
"""
                report_bytes = report_text.encode("utf-8")
                report_fn = f"Report_{student_id}.txt"
                upload_file_to_drive(report_bytes, report_fn, DRIVE_FOLDER_ID, "text/plain")

                st.session_state.graded_results.append({
                    "student_id": student_id,
                    "file_name": file.name,
                    "file_bytes": file_bytes,
                    "mime_type": mime_type,
                    "final_score": final_score,
                    "total_scale": total_rubric_scale,
                    "word_count": word_count,
                    "scores": [g_score, o_score, c_score],
                    "res_g": res_g,
                    "res_o": res_o,
                    "res_c": res_c,
                    "report_bytes": report_bytes,
                    "report_fn": report_fn,
                    "question": active_question
                })

        if st.session_state.graded_results:
            st.success("✅ Evaluation Complete! Switch to **Step 3** tab to view grades and download reports.")

# --- TAB 3: EVALUATION & REPORTS ---
with wizard_tab3:
    if not st.session_state.graded_results:
        st.info("📌 **No evaluation results yet.** Run evaluations in **Step 2** to view student grades here.")
    else:
        graded_results = st.session_state.graded_results

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for item in graded_results:
                zip_file.writestr(item["report_fn"], item["report_bytes"])
        zip_buffer.seek(0)

        st.download_button(
            label="📦 Download All Student Reports (ZIP Batch)",
            data=zip_buffer.getvalue(),
            file_name="Class_Grading_Reports.zip",
            mime="application/zip",
            type="primary",
            use_container_width=True
        )
        st.markdown("<br>", unsafe_allow_html=True)

        for item in graded_results:
            scale_val = item.get("total_scale", 100)
            with st.expander(f"✅ Graded: {item['student_id']} | Final Score: {item['final_score']} / {scale_val}", expanded=True):
                col_canvas, col_details = st.columns([1, 1])

                with col_canvas:
                    st.markdown("#### 📄 Document Canvas Preview")
                    if "image" in item["mime_type"]:
                        st.image(item["file_bytes"], use_container_width=True)
                    elif item["mime_type"] == "application/pdf":
                        b64_pdf = base64.b64encode(item["file_bytes"]).decode("utf-8")
                        pdf_display = f'''
                            <object data="data:application/pdf;base64,{b64_pdf}" type="application/pdf" width="100%" height="500px">
                                <p>PDF preview not supported.</p>
                            </object>
                        '''
                        st.markdown(pdf_display, unsafe_allow_html=True)
                    else:
                        st.text_area("Plain Text Submission Preview", value=item["file_bytes"].decode("utf-8", errors="ignore"), height=300)

                with col_details:
                    st.markdown("#### 🎯 Evaluation Breakdown")
                    st.markdown(f"**Target Question:** *\"{item.get('question', 'N/A')}\"*")
                    st.markdown(f"**Final Score:** `{item['final_score']} / {scale_val}` | **Word Count:** `{item['word_count']}`")
                    st.markdown(f"**Gemini:** {item['scores'][0]} | **GPT-4o:** {item['scores'][1]} | **Claude:** {item['scores'][2]}")

                    st.download_button(f"📥 Download Report ({item['report_fn']})", item['report_bytes'], item['report_fn'], "text/plain", use_container_width=True)

                    st.divider()
                    t1, t2, t3 = st.tabs(["🤖 Gemini", "🧠 GPT-4o", "🦉 Claude"])
                    with t1: st.json(item['res_g'])
                    with t2: st.json(item['res_o'])
                    with t3: st.json(item['res_c'])
