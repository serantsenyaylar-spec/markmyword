#!/usr/bin/env python3
import io
import json
import logging
import os
import random
import re
import time
import zipfile

logger = logging.getLogger(__name__)

try:
    import anthropic
except ImportError:
    anthropic = None

import pandas as pd
import requests
import streamlit as st
from google import genai  # Using only the new SDK
from google.genai import types
from groq import Groq
from pydantic import BaseModel
from pypdf import PdfReader
from supabase import create_client, Client
import gspread
from authlib.integrations.httpx_client import AsyncOAuthClient
import docx2txt
from PIL import Image
import pytesseract

# --- UTILITY FUNCTIONS ---
def get_secret(secret_name: str):
    """Safely retrieves secrets from Streamlit's secret manager or environment.
    
    Streamlit's st.secrets.get() is safe and explicitly handles the common case
    when a secret is missing. No raw st.secrets access bypasses this handling.
    """
    try:
        return st.secrets.get(secret_name)
    except StreamlitSecretNotFoundError:
        # Secret not found — return None or a safe default
        return None

def setup_logging():
    """Configures logging format and level."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

# --- GOOGLE GENAI CONFIG ---
def configure_google_genai():
    """Configures the Google GenAI client."""
    api_key = get_secret("GEMINI_API_KEY")
    if api_key:
        return genai.Client(api_key=api_key)
    return None

# --- GROQ CONFIG ---
def configure_groq():
    """Configures the Groq client."""
    api_key = get_secret("GROQ_API_KEY")
    if api_key:
        return Groq(api_key=api_key)
    return None

# --- SUPABASE CONFIG ---
def get_supabase_client() -> Client:
    """Returns a Supabase client, or None if secrets are missing."""
    url = get_secret("SUPABASE_URL")
    key = get_secret("SUPABASE_KEY")
    if url and key:
        return create_client(url, key)
    return None

def log_user_session():
    """Logs the user session to Supabase for audit and usage tracking."""
    try:
        supabase: Client = get_supabase_client()
        if not supabase:
            logger.debug("Supabase not configured; skipping session log.")
            return
        
        user_email = st.session_state.get("login_email", "unknown")
        app_mode = st.session_state.get("app_mode", "unknown")
        
        data = {
            "user_email": user_email,
            "app_mode": app_mode,
            "timestamp": time.time(),
        }
        supabase.table("session_log").insert(data).execute()
    except Exception as e:
        logger.warning("Error logging session: %s", e)

# --- UPLOAD PARSING LIMITS -------------------------------------------------
# A .docx is a ZIP, so its "signature" alone proves very little: an .xlsx, a
# .jar or a decompression bomb renamed to .docx all start with PK\x03\x04.
# These bounds keep one crafted file from expanding into memory during parsing
# or flooding a model prompt afterwards.
DOCX_DOCUMENT_PART = "word/document.xml"
MAX_DOCX_MEMBERS = 1000
MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_EXTRACTED_CHARS = 60_000
# zipfile raises several unrelated types for hostile or truncated archives.
ZIP_PARSE_ERRORS = (zipfile.BadZipFile, OSError, RuntimeError, ValueError, NotImplementedError)


def _is_word_document(file_bytes: bytes) -> bool:
    """True only for a readable ZIP that really carries a Word document part."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            return DOCX_DOCUMENT_PART in set(archive.namelist())
    except ZIP_PARSE_ERRORS:
        # BadZipFile/OSError for a truncated or non-archive file, RuntimeError for
        # an encrypted member, ValueError/NotImplementedError for an exotic one.
        logger.debug("Upload failed the DOCX container check.", exc_info=True)
        return False


def _docx_size_violation(file_bytes: bytes) -> str:
    """Human-readable reason a .docx is too big to unpack safely, else "".

    Checked against the archive's central directory before docx2txt touches it,
    so an oversized archive is refused rather than expanded.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            members = archive.infolist()
    except ZIP_PARSE_ERRORS:
        return "unreadable archive"

    if len(members) > MAX_DOCX_MEMBERS:
        return f"{len(members)} archive entries (limit {MAX_DOCX_MEMBERS})"

    total = sum(member.file_size for member in members)
    if total > MAX_DOCX_UNCOMPRESSED_BYTES:
        return (
            f"{total / (1024 * 1024):.0f} MB uncompressed "
            f"(limit {MAX_DOCX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB)"
        )
    return ""


def _clamp_extracted_text(text: str, filename: str) -> str:
    """Bounds one submission's extracted text so it cannot flood a model prompt."""
    text = (text or "").strip()
    if len(text) <= MAX_EXTRACTED_CHARS:
        return text
    st.warning(
        f"⚠️ **{filename}**: only the first {MAX_EXTRACTED_CHARS:,} of "
        f"{len(text):,} characters were kept for grading."
    )
    return text[:MAX_EXTRACTED_CHARS]


def _looks_like_extension(file_bytes: bytes, file_extension: str) -> bool:
    """Validates the upload by content signature, not just by its filename suffix.

    Returns True when the content signature matches the claimed extension.
    TXT has no reliable signature and is always allowed.
    """
    signatures = {
        'pdf': b'%PDF',
        'png': b'\x89PNG',
        'jpg': b'\xff\xd8\xff',
        'jpeg': b'\xff\xd8\xff',
        'docx': b'PK\x03\x04',  # ZIP signature (for a pre-check before DOCX-specific validation)
    }
    expected = signatures.get(file_extension)
    if expected is None:
        return True  # txt: no signature to check
    if file_extension == "docx":
        # The ZIP magic alone would accept any archive (or bare garbage) renamed
        # to .docx, so require the Word document part to actually be present.
        return _is_word_document(file_bytes)
    if file_extension == "pdf":
        # The PDF header may appear within the first 1024 bytes of the file.
        return expected in file_bytes[:1024]
    # PNG, JPG, JPEG: check the first few bytes
    return file_bytes.startswith(expected)

# --- PDF PROCESSING ---
MIN_CHARS_PER_PAGE_FOR_TEXT_LAYER = 50
MAX_PDF_PAGES = 200

def _extract_meta(source: str, error_code: str = None) -> dict:
    """Build a metadata dict for extraction results."""
    return {"source": source, "error_code": error_code}

def extract_text_from_image(uploaded_file):
    """Extracts text from PNG/JPG using pytesseract (OCR)."""
    try:
        image = Image.open(io.BytesIO(uploaded_file.getbuffer()))
        text = pytesseract.image_to_string(image)
        return text.strip() or "", _extract_meta("ocr")
    except Exception as e:
        logger.error("OCR error: %s", e)
        return "", _extract_meta("none", "ocr_failed")

def _extract_text_from_pdf(file_bytes: bytes, filename: str):
    """Extracts text from PDF using pypdf, with fallback to OCR if needed."""
    text = ""
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        page_count = len(reader.pages)
        
        if page_count > MAX_PDF_PAGES:
            st.error(
                f"⚠️ {filename} has {page_count} pages (limit {MAX_PDF_PAGES}). Skipped."
            )
            return "", _extract_meta("none", "pdf_too_long")
        
        for page in reader.pages:
            text += page.extract_text() + "\n"
    except Exception as e:
        logger.error("PDF extraction error: %s", e)
        return "", _extract_meta("none", "pdf_parse_failed")

    # A digital PDF (Word/Docs export) already has everything we need.
    if text and len(text) >= MIN_CHARS_PER_PAGE_FOR_TEXT_LAYER * max(page_count, 1):
        return _clamp_extracted_text(text, filename), _extract_meta("text_layer")

    if page_count > MAX_PDF_PAGES:
        st.error(
            f"⚠️ {filename} has {page_count} pages (limit {MAX_PDF_PAGES}). Skipped."
        )
        return "", _extract_meta("none", "pdf_too_long")
    
    return "", _extract_meta("none", "no_text_layer")

def extract_text_with_status(uploaded_file):
    """Dispatches to the appropriate text extractor based on file type."""
    try:
        file_bytes = uploaded_file.read()
        file_extension = uploaded_file.name.split('.')[-1].lower()
        
        # Validate content signature
        if not _looks_like_extension(file_bytes, file_extension):
            st.error(
                f"⚠️ {uploaded_file.name} signature doesn't match .{file_extension}. Skipped."
            )
            return "", _extract_meta("none", "invalid_signature")
        
        if file_extension == 'pdf':
            return _extract_text_from_pdf(file_bytes, uploaded_file.name)
        elif file_extension == 'docx':
            # Refuse an archive that would expand without bound before unpacking it.
            violation = _docx_size_violation(file_bytes)
            if violation:
                st.error(
                    f"⚠️ {uploaded_file.name} is not a .docx this app can parse safely "
                    f"({violation}). Skipped."
                )
                return "", _extract_meta("none", "unsafe_docx")
            extracted = docx2txt.process(io.BytesIO(file_bytes)) or ""
            return _clamp_extracted_text(extracted, uploaded_file.name), _extract_meta("document")
        elif file_extension == 'txt':
            extracted = file_bytes.decode("utf-8", errors="ignore")
            return _clamp_extracted_text(extracted, uploaded_file.name), _extract_meta("document")
        elif file_extension in ('png', 'jpg', 'jpeg'):
            return extract_text_from_image(uploaded_file)
        return "", _extract_meta("none", "unsupported_type")
    except Exception as e:
        logger.error("Error extracting text: %s", e)
        st.error(f"⚠️ Error processing {uploaded_file.name}. Skipped.")
        return "", _extract_meta("none", "extraction_error")

# --- AI MODEL INTEGRATION ---
def get_google_credentials():
    """Returns Google service account credentials for Sheets access."""
    try:
        from google.oauth2.service_account import Credentials
        creds_secret = get_secret("gcp_service_account")
        if not creds_secret:
            return None
        
        creds_json = json.loads(creds_secret) if isinstance(creds_secret, str) else dict(creds_secret)
        # Least-privilege scope: the grading spreadsheet only. The drive.file
        # scope went with the unused Drive-upload helper, so these credentials
        # can no longer touch any Drive file.
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
        ]
        return Credentials.from_service_account_info(creds_json, scopes=scopes)
    except Exception as e:
        logger.error("Google credentials error: %s", e)
        return None
        
# --- CONFIGURATION & CONSTANTS ---
SHEET_ID = get_secret("SHEET_ID")

raw_admins = get_secret("ADMIN_EMAILS")
ADMIN_EMAILS = [email.strip() for email in (raw_admins or "").split(",") if email.strip()]

APP_ENV = os.getenv("APP_ENV", "development")
DEBUG = APP_ENV == "development"

# --- STREAMLIT CONFIG ---
st.set_page_config(
    page_title="Mark My Words",
    page_icon="✍️",
    layout="wide",
    initial_sidebar_state="expanded"
)

setup_logging()

# --- DEFAULT SESSION STATE ---
default_states = {
    "app_mode": "student",
    "login_email": "",
    "login_notified": False,
    "uploaded_files": [],
    "submission_text": "",
    "grade_score": None,
    "word_count": 0,
}

for key, val in default_states.items():
    if key not in st.session_state:
        st.session_state[key] = val
        
# --- GOOGLE WORKSPACE SHEETS ---
def save_grade(user_name, user_email, student_id, assignment_type, final_score, word_count, total_scale):
    """Appends a new row to the Google Sheet with grading results."""
    try:
        if not SHEET_ID:
            logger.warning("SHEET_ID not configured.")
            return False
        
        creds = get_google_credentials()
        if not creds:
            logger.error("Could not obtain Google credentials.")
            return False
        
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(SHEET_ID).sheet1
        
        new_row = [user_name, user_email, student_id, assignment_type, final_score, word_count, total_scale, time.time()]
        sheet.append_row(new_row)
        return True
    except Exception as e:
        logger.error("Error saving grade to sheet: %s", e)
        return False

# --- MAIN UI ---
st.title("✍️ Mark My Words")
st.markdown("**AI-powered essay grading for İSTEK Schools**")

# Mode selection
mode = st.radio("Select mode:", ["Student Submission", "Teacher Dashboard"], key="app_mode_selector")

if mode == "Student Submission":
    st.header("Submit Your Essay")
    
    # Login
    with st.form("login_form"):
        email = st.text_input("Your email:")
        submitted = st.form_submit_button("Login")
        if submitted and email:
            st.session_state.login_email = email
            st.session_state.login_notified = True
            st.success(f"Logged in as {email}")
    
    if st.session_state.login_email:
        # File upload
        uploaded_files = st.file_uploader(
            "Upload your essay (PDF, DOCX, TXT, PNG, JPG):",
            type=["pdf", "docx", "txt", "png", "jpg"],
            accept_multiple_files=False
        )
        
        if uploaded_files:
            st.session_state.uploaded_files = [uploaded_files]
            text, meta = extract_text_with_status(uploaded_files)
            
            if text:
                st.session_state.submission_text = text
                st.info(f"✅ Extracted {len(text)} characters from {meta['source']}")
                
                # Word count
                word_count = len(text.split())
                st.session_state.word_count = word_count
                st.write(f"**Word count:** {word_count}")
                
                # Grading (simplified for demo)
                if st.button("Get AI Feedback"):
                    # This would call the actual grading model
                    st.info("Grading in progress...")
            else:
                st.warning(f"Could not extract text from file. Error: {meta.get('error_code')}")
else:
    st.header("Teacher Dashboard")
    
    # Teacher login
    teacher_email = st.text_input("Teacher email:")
    if teacher_email in ADMIN_EMAILS:
        st.success(f"✅ Logged in as teacher")
        st.write("Dashboard features will go here.")
    else:
        st.error("Unauthorized access.")

# Footer
st.markdown("""
---
<div style="text-align: center; padding: 20px; color: #666;">
    <p>&copy; 2026 Serant Şenyaylar. All rights reserved. Created for İSTEK Schools.</p>
</div>
""", unsafe_allow_html=True)
