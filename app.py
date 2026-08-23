import streamlit as st
from google import genai
from google.genai import types
import pandas as pd
import os
import sqlite3
import re

# --- PAGE SETTINGS & BRANDING ---
# initial_sidebar_state="collapsed" hides it by default but keeps the arrow working perfectly!
st.set_page_config(page_title="Mark My Words", page_icon="📝", layout="wide", initial_sidebar_state="collapsed")

# Injecting HIGHLY SAFE Custom CSS
st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;700&display=swap');
    
    /* Safely apply font ONLY to paragraphs and headers. 
       We leave spans and labels alone so Streamlit's double-arrow and icons don't break! */
    p, h1, h2, h3, h4, h5, h6 {
        font-family: 'Roboto', sans-serif !important;
    }
    
    /* Force Pure White Main Background */
    [data-testid="stAppViewContainer"] {
        background-color: #FFFFFF !important;
    }
    
    /* Light grey sidebar for contrast when opened */
    [data-testid="stSidebar"] {
        background-color: #F8F9FA !important;
    }
    
    /* İSTEK Brand Colors for Headers */
    h1, h2, h3, h4, h5, h6 { 
        color: #0055A5 !important; 
    }
    
    /* Target ONLY the primary blue evaluation button */
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
    """Returns `True` if the user had the correct password."""
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if not st.session_state.authenticated:
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            st.warning("🔒 **Restricted Access:** Teacher Portal Only")
            password = st.text_input("Enter School Passcode:", type="password")
            if st.button("Login", type="primary", use_container_width=True):
                # CHANGE "ISTEKTEACHER" TO WHATEVER PASSWORD YOU WANT
                if password == "ISTEKTEACHER": 
                    st.session_state.authenticated = True
                    st.rerun()
                else:
                    st.error("😕 Incorrect passcode.")
        st.stop()  # Do not continue running the rest of the app!

# Call the security check before anything else loads
check_password()
# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect('gradebook.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS grades 
                 (id INTEGER PRIMARY KEY, student TEXT, assignment TEXT, score REAL, word_count TEXT)''')
    conn.commit()
    conn.close()

def save_grade(student, assignment, score, word_count):
    conn = sqlite3.connect('gradebook.db')
    c = conn.cursor()
    c.execute("INSERT INTO grades (student, assignment, score, word_count) VALUES (?, ?, ?, ?)", 
              (student, assignment, score, word_count))
    conn.commit()
    conn.close()

def load_grades():
    conn = sqlite3.connect('gradebook.db')
    df = pd.read_sql_query("SELECT student as 'Student', assignment as 'Assignment', score as 'Score', word_count as 'Word Count' FROM grades", conn)
    conn.close()
    return df

def clear_db():
    conn = sqlite3.connect('gradebook.db')
    c = conn.cursor()
    c.execute("DELETE FROM grades")
    conn.commit()
    conn.close()

init_db()

# --- SIDEBAR: SETTINGS ---
st.sidebar.header("⚙️ App Settings")
api_key = st.sidebar.text_input("Enter Gemini API Key:", value="", type="password")

st.sidebar.markdown("---")
st.sidebar.subheader("📂 Custom Rubric")
custom_rubric_file = st.sidebar.file_uploader("Upload Custom CSV Rubric", type=["csv"])

st.sidebar.markdown("---")
if st.sidebar.button("🗑️ Clear Master Database", use_container_width=True):
    clear_db()
    st.sidebar.success("Database cleared!")
    st.rerun()

# --- UI HEADER ---
col_logo, col_title = st.columns([1, 4])
with col_logo:
    if os.path.exists("kurum_genel_logo_2_eng.png"):
        st.image("kurum_genel_logo_2_eng.png", use_container_width=True)
    else:
        uploaded_logo = st.file_uploader("Upload Logo Here to Save it:", type=["png", "jpg", "jpeg"])
        if uploaded_logo:
            with open("kurum_genel_logo_2_eng.png", "wb") as f:
                f.write(uploaded_logo.getbuffer())
            st.rerun()

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
        if not api_key:
            st.error("Please open the sidebar (top left arrow) and enter your API Key.")
        elif not uploaded_pdfs:
            st.error("Please upload at least one PDF file.")
        else:
            if custom_rubric_file:
                rubric_text = pd.read_csv(custom_rubric_file).to_string()
            else:
                filename = "Rubric_GUIDED_ESSAY_WRITING_B1.csv" if "Essay" in assignment_type else "Rubric_GUIDED_PARAGRAPH_WRITING_B1.csv"
                if os.path.exists(filename):
                    rubric_text = pd.read_csv(filename).to_string()
                else:
                    st.error(f"Missing default rubric: {filename}. Please upload one in the sidebar.")
                    st.stop()
                    
            client = genai.Client(api_key=api_key)
            
            for pdf_file in uploaded_pdfs:
                student_identifier = pdf_file.name.replace('.pdf', '')
                pdf_bytes = pdf_file.getvalue()
                
                with st.spinner(f"Evaluating {pdf_file.name}..."):
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
                        document_part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
                        response = client.models.generate_content(model="gemini-2.5-flash", contents=[prompt, document_part])
                        
                        full_text = response.text
                        
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
                        save_grade(student_identifier, assignment_type, float(score), word_count)
                        
                        with st.expander(f"✅ Graded: {student_identifier} (Score: {score})", expanded=False):
                            st.markdown(display_text)
                            
                    except Exception as e:
                        st.error(f"Failed to grade {pdf_file.name}: {str(e)}")
            
            st.rerun()

with col2:
    st.markdown("<h3 class='gradebook-header'>📈 Class Analytics Dashboard</h3>", unsafe_allow_html=True)
    df_grades = load_grades()
    
    if not df_grades.empty:
        col_avg, col_count = st.columns(2)
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
