import streamlit as st
from google import genai
import pandas as pd
from PIL import Image
import os

# Set page configuration
st.set_page_config(page_title="B1+ Automated Writing Evaluator", page_icon="📝", layout="centered")

st.title("📝 B1+ English Writing Grader")
st.write("Upload a photo/scan of a student's handwritten writing or paste text to evaluate it automatically against your rubrics.")

# Sidebar for API Key & Settings
st.sidebar.header("Settings")
api_key = st.sidebar.text_input("Gemini API Key", type="password")

assignment_type = st.sidebar.selectbox(
    "Select Assignment Type",
    ["Guided Essay Writing (120–150 words)", "Guided Paragraph Writing (70–90 words)"]
)

def load_rubric(task_type):
    if "Essay" in task_type:
        filename = "Rubric_GUIDED_ESSAY_WRITING_B1.csv"
    else:
        filename = "Rubric_GUIDED_PARAGRAPH_WRITING_B1.csv"
    
    if os.path.exists(filename):
        df = pd.read_csv(filename)
        return df.to_string()
    return None

input_mode = st.radio("Select Input Method:", ["Upload Image (Scanned Handwriting)", "Paste Text"])

student_image = None
student_text = ""

if input_mode == "Upload Image (Scanned Handwriting)":
    uploaded_file = st.file_uploader("Choose an image file...", type=["jpg", "jpeg", "png"])
    if uploaded_file:
        student_image = Image.open(uploaded_file)
        st.image(student_image, caption="Uploaded Document", use_container_width=True)
else:
    student_text = st.text_area("Paste Student Writing Here:", height=200)

if st.button("Evaluate & Grade Submission", type="primary"):
    if not api_key:
        st.error("Please enter your Gemini API Key in the sidebar on the left.")
    else:
        rubric_text = load_rubric(assignment_type)
        if not rubric_text:
            st.error("Rubric CSV file not found! Make sure your rubric CSV files are in the same folder.")
        else:
            client = genai.Client(api_key=api_key)
            
            with st.spinner("Reading paper and applying rubric evaluation..."):
                prompt = f"""
                You are an expert high school English teacher evaluating a B1+ writing assignment.
                Assignment Type: {assignment_type}
                
                Strictly apply the following scoring criteria:
                {rubric_text}
                
                Instructions:
                1. If an image is provided, accurately transcribe the handwritten text first.
                2. Calculate the exact word count.
                3. Evaluate the student strictly across: Task Achievement, Organization & Style, and Accuracy.
                4. Present the output clearly structured using these headings:
                   ### 📜 Transcribed Text
                   ### 📊 Word Count & Rule Compliance
                   ### 🏆 Score Breakdown (Points per category)
                   ### 💬 Detailed Pedagogical Feedback
                """

                contents = [prompt]
                if input_mode == "Upload Image (Scanned Handwriting)" and student_image:
                    contents.append(student_image)
                elif student_text.strip():
                    contents.append(f"STUDENT TEXT:\n{student_text}")
                else:
                    st.warning("Please upload an image or enter text before grading.")
                    st.stop()

                try:
                    response = client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=contents
                    )
                    st.success("Evaluation Completed!")
                    st.markdown(response.text)
                except Exception as e:
                    st.error(f"Error during AI processing: {e}")
