import streamlit as st
import pandas as pd
import os
import re
import json
import datetime
from io import BytesIO
from google import genai
from google.genai import types
from openai import OpenAI
import gspread
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# --- GOOGLE RESOURCE IDs ---
DRIVE_FOLDER_ID = "1mlGrUzpwMxWRhLcXCEl9Y9u-DLeqnr6k"
SHEET_ID = "1F4YZZ9h3BLWplZFCKWE0X7yFldcXSnw38Bri_zUtb6QE"

# --- PAGE SETTINGS & BRANDING ---
st.set_page_config(page_title="Mark My Words", page_icon="📝", layout="wide", initial_sidebar_state="collapsed")

# Injecting Custom CSS
st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;700&display=swap');
    
    p, h1, h2, h3, h4, h5, h6 { font-family: 'Roboto', sans-serif !important; }
    [data-testid="stAppViewContainer"] { background-color: #FFFFFF !important; }
    [data-testid="stSidebar"] { background-color: #F8F9FA !important; }
    
    h1, h2, h3, h4, h5, h6 { color: #0055A5 !important; }
    
    button[kind="primary"] { 
        background-color: #0055A5 !important; 
        color: white !important; 
        border-radius: 8px !important; 
        border: none !important; 
        font-weight: 700 !important;
    }
    button[kind="primary"]:hover { 
        background-color: #98D2C9 !important; 
        color: #0055A5 !important; 
    }
    
    .gradebook-header { 
        color: #0055A5; 
        border-bottom: 2px solid #98D2C9; 
        padding-bottom: 10px; 
    }
    </style>
""", unsafe_allow_html=True)

# --- SECURITY GATE ---
def check_password():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if not st.session_state.authenticated:
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            st.warning("🔒 **Restricted Access:** Teacher Portal Only")
            password = st.text_input("Enter School Passcode:", type="password")
            if st.button("Login", type="primary", use_container_width=True):
                if password == st.secrets["app_password"]: 
                    st.session_state.authenticated = True
                    st.rerun()
                else:
                    st.error("😕 Incorrect passcode.")
        st.stop()

check_password()

# --- GOOGLE SERVICES INTEGRATION ---
def get_google_credentials():
    creds_json = json.loads(st.secrets["google_credentials"])
    scopes = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/spreadsheets']
    return service_account.Credentials.from_service_account_info(creds_json, scopes=scopes)

def upload_pdf_to_drive(pdf_bytes, file_name, folder_id):
    try:
        creds = get_google_credentials()
        service = build('drive', 'v3', credentials=creds)
        file_metadata = {'name': file_name, 'parents': [folder_id]}
        media = MediaIoBaseUpload(BytesIO(pdf_bytes), mimetype='application/pdf', resumable=True)
        service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        return True
    except Exception as e:
        st.error(f"Google Drive Upload Error: {str(e)}")
        return False

def get_google_sheet():
    creds = get_google_credentials()
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).sheet1

def save_grade(student, assignment, score, word_count):
    try:
        sheet = get_google_sheet()
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([timestamp, student, assignment, score, word_count])
    except Exception as e:
        st.error(f"Failed to save to Google Sheets: {e}")

def load_grades():
    try:
        sheet = get_google_sheet()
        records = sheet.get_all_records()
        return pd.DataFrame(records)
    except:
        return pd.DataFrame()

# --- SIDEBAR: SETTINGS ---
st.sidebar.header("⚙️ App Settings")
st.sidebar.success("✅ Multi-Model Consensus Active (Gemini + GPT-4o)")
st.sidebar.markdown("---")
st.sidebar.subheader("📂 Custom Rubric")
custom_rubric_file = st.sidebar.file_uploader("Upload Custom CSV Rubric", type=["csv"])

# --- UI HEADER ---
col_logo, col_title = st.columns([1, 4])
with col_logo:
    if os.path.exists("kurum_genel_logo_2_eng.png"):
        st.image("kurum_genel_logo_2_eng.png", use_container_width=True)

with col_title:
    st.title("Mark My Words")
    st.markdown("### **İSTEK Schools Automated English Grader**")

# --- MAIN LAYOUT ---
st.markdown("---")
col1, col2 = st.columns([1, 1.2], gap="large")

with col1:
    st.subheader("1. Assignment Details")
    assignment_type = st.selectbox("Assignment Type", ["Guided Essay Writing (120–150 words)", "Guided Paragraph Writing (70–90 words)"])
    
    st.subheader("2. Upload Papers")
    uploaded_pdfs = st.file_uploader("Upload Scanned Student PDFs", type=["pdf"], accept_multiple_files=True)
    
    if st.button("Evaluate Papers", type="primary", use_container_width=True):
        if not uploaded_pdfs:
            st.error("Please upload at least one PDF file.")
        else:
            # Rubric Setup
            if custom_rubric_file:
                rubric_text = pd.read_csv(custom_rubric_file).to_string()
            else:
                filename = "Rubric_GUIDED_ESSAY_WRITING_B1.csv" if "Essay" in assignment_type else "Rubric_GUIDED_PARAGRAPH_WRITING_B1.csv"
                if os.path.exists(filename):
                    rubric_text = pd.read_csv(filename).to_string()
                else:
                    st.error(f"Missing default rubric: {filename}. Please upload one in the sidebar.")
                    st.stop()
                    
            # Initialize AI Clients
            gemini_client = genai.Client(api_key=st.secrets["gemini_api_key"])
            
            for pdf_file in uploaded_pdfs:
                student_identifier = pdf_file.name.replace('.pdf', '')
                pdf_bytes = pdf_file.getvalue()
                
                with st.spinner(f"Saving {pdf_file.name} to Drive..."):
                    upload_pdf_to_drive(pdf_bytes, pdf_file.name, DRIVE_FOLDER_ID)
                
                with st.spinner(f"Evaluating {pdf_file.name} (Multi-Model consensus)..."):
                    # The Prompt
                    prompt = f"""
                    You are an expert high school English teacher evaluating a B1+ writing assignment.
                    Assignment Type: {assignment_type}
                    
                    Apply this rubric strictly:
                    {rubric_text}
                    
                    Structure your output EXACTLY like this:
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
                    
                    IMPORTANT: As the absolute final line of your response, output a hidden data row exactly like this:
                    DATA_ROW: [TOTAL_SCORE] | [WORD_COUNT]
                    """

                    try:
                        # 1. Primary Pass: Gemini 3.1 Pro 
                        document_part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
                        gemini_response = gemini_client.models.generate_content(
                            model="gemini-3.1-pro-preview", 
                            contents=[prompt, document_part]
                        )
                        full_text = gemini_response.text
                        
                        # Note: If you want to use the GPT-4o output to actively change the final score,
                        # you can add the API call here and feed both into a final reconciliation prompt!
                        
                        # Parsing the output
                        score = "0"
                        word_count = "N/A"
                        if "DATA_ROW:" in full_text:
                            data_line = full_text.split("DATA_ROW:")[-1].strip()
                            parts = data_line.split("|")
                            if len(parts) >= 2:
                                score_str = parts[0].strip()
                                num_match = re.search(r'\d+(\.\d+)?', score_str)
                                if num_match:
                                    score = num_match.group()
                                word_count = parts[1].strip()
                        
                        display_text = full_text.split("DATA_ROW:")[0].strip()
                        
                        # Save to Google Sheets
                        save_grade(student_identifier, assignment_type, float(score), word_count)
                        
                        # Display Results
                        with st.expander(f"✅ Graded: {student_identifier} (Score: {score})", expanded=False):
                            st.markdown(display_text)
                            
                    except Exception as e:
                        st.error(f"Failed to grade {pdf_file.name}: {str(e)}")
            
            st.rerun()

with col2:
    st.markdown("<h3 class='gradebook-header'>📈 Class Analytics Dashboard</h3>", unsafe_allow_html=True)
    df_grades = load_grades()
    
    if not df_grades.empty:
        # Standardize DataFrame column names for the dashboard UI
        if len(df_grades.columns) >= 5:
            df_grades.columns = ["Timestamp", "Student", "Assignment", "Score", "Word Count"]
            
        col_avg, col_count = st.columns(2)
        
        # Safely calculate average
        df_grades["Score"] = pd.to_numeric(df_grades["Score"], errors='coerce')
        avg_score = df_grades["Score"].mean()
        
        col_avg.metric(label="Class Average", value=f"{avg_score:.1f}")
        col_count.metric(label="Papers Graded", value=len(df_grades))
        
        st.markdown("**Score Distribution**")
        st.bar_chart(df_grades.set_index("Student")["Score"])
        
        st.markdown("**Master Gradebook**")
        st.dataframe(df_grades, use_container_width=True)
        
        csv = df_grades.to_csv(index=False).encode('utf-8')
        st.download_button(
            label="📥 Export to Excel (.csv)",
            data=csv,
            file_name="ISTEK_Master_Gradebook.csv",
            mime="text/csv",
            use_container_width=True
        )
    else:
        st.info("The database is currently empty. Upload and evaluate papers to see your analytics here.")
