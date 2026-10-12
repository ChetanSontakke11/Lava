"""RAGVoice: local-first document Q&A with optional speech input/output.

Run with:
    streamlit run app.py

Uses a local Ollama server for LLM inference and TF-IDF for retrieval.
"""
from __future__ import annotations
import io
import os
import tempfile
from pathlib import Path

import requests
import streamlit as st
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

APP_TITLE = "RAGVoice"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
CHUNK_SIZE = 900
CHUNK_OVERLAP = 150


def extract_text(uploaded_file) -> str:
    suffix = Path(uploaded_file.name).suffix.lower()
    raw = uploaded_file.getvalue()
    if suffix in {".txt", ".md", ".csv", ".json", ".py"}:
        return raw.decode("utf-8", errors="replace")
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if suffix == ".docx":
        from docx import Document
        document = Document(io.BytesIO(raw))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    raise ValueError(f"Unsupported file type: {suffix or 'unknown'}")


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    cleaned = " ".join(text.split())
    if not cleaned:
        return []
    chunks, start = [], 0
    while start < len(cleaned):
        end = min(start + size, len(cleaned))
        chunks.append(cleaned[start:end])
        if end == len(cleaned):
            break
        start = max(end - overlap, start + 1)
    return chunks


def build_index(chunks: list[str]):
    if not chunks:
        return None, None
    vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2))
    return vectorizer, vectorizer.fit_transform(chunks)


def retrieve(question: str, chunks: list[str], vectorizer, matrix, top_k: int = 4) -> list[str]:
    if not chunks or vectorizer is None or matrix is None:
        return []
    scores = cosine_similarity(vectorizer.transform([question]), matrix).ravel()
    return [chunks[i] for i in scores.argsort()[::-1][:top_k] if scores[i] > 0]


def ollama_chat(prompt: str, model: str, system_prompt: str, base_url: str) -> str:
    response = requests.post(
        f"{base_url.rstrip('/')}/api/chat",
        json={"model": model, "stream": False, "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]},
        timeout=180,
    )
    response.raise_for_status()
    return response.json().get("message", {}).get("content", "").strip() or "The model returned an empty response."


def transcribe_audio(audio_bytes: bytes, file_suffix: str) -> str:
    from faster_whisper import WhisperModel
    suffix = file_suffix or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        audio_path = tmp.name
    try:
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(audio_path, vad_filter=True)
        return " ".join(segment.text.strip() for segment in segments).strip()
    finally:
        try:
            os.remove(audio_path)
        except OSError:
            pass


def speak_text(text: str) -> None:
    import pyttsx3
    engine = pyttsx3.init()
    engine.setProperty("rate", 175)
    engine.say(text)
    engine.runAndWait()
    engine.stop()


st.set_page_config(page_title=APP_TITLE, page_icon="🎙️", layout="wide")
st.title("🎙️ RAGVoice")
st.caption("A local-first voice assistant for asking questions about your documents.")
st.info(
    "Document retrieval and Ollama inference run locally. Initial setup may require "
    "downloading Python packages and AI models. To verify offline operation, disable "
    "network access after setup."
)

with st.sidebar:
    st.header("Settings")
    model_name = st.text_input("Ollama model", value=DEFAULT_MODEL)
    ollama_url = st.text_input("Ollama URL", value=OLLAMA_URL)
    st.markdown("Start Ollama and download a model before asking questions:")
    st.code("ollama pull llama3.2\nollama serve\nstreamlit run app.py", language="bash")
    st.caption("Use a model name that is already installed locally.")

if "chunks" not in st.session_state:
    st.session_state.chunks = []
if "sources" not in st.session_state:
    st.session_state.sources = []
if "history" not in st.session_state:
    st.session_state.history = []

tab_chat, tab_docs, tab_voice = st.tabs(["Ask RAGVoice", "Knowledge Base", "Voice"])

with tab_docs:
    st.subheader("Add documents")
    files = st.file_uploader(
        "Upload TXT, Markdown, PDF, DOCX, CSV, JSON, or Python files",
        type=["txt", "md", "pdf", "docx", "csv", "json", "py"],
        accept_multiple_files=True,
    )
    if st.button("Process documents", type="primary", disabled=not files):
        new_chunks, new_sources, errors = [], [], []
        progress = st.progress(0)
        for index, uploaded in enumerate(files):
            try:
                pieces = chunk_text(extract_text(uploaded))
                new_chunks.extend(pieces)
                new_sources.extend([uploaded.name] * len(pieces))
                if not pieces:
                    errors.append(f"{uploaded.name}: no readable text found.")
            except Exception as exc:
                errors.append(f"{uploaded.name}: {exc}")
            progress.progress((index + 1) / len(files))
        if new_chunks:
            st.session_state.chunks = new_chunks
            st.session_state.sources = new_sources
            st.success(f"Indexed {len(new_chunks)} chunks from {len(set(new_sources))} file(s).")
        for error in errors:
            st.warning(error)
    if st.session_state.chunks:
        st.metric("Indexed text chunks", len(st.session_state.chunks))
        st.write("Sources:", ", ".join(sorted(set(st.session_state.sources))))
        if st.button("Clear knowledge base"):
            st.session_state.chunks = []
            st.session_state.sources = []
            st.rerun()
    else:
        st.caption("Upload documents and process them to enable document-grounded answers.")

with tab_chat:
    st.subheader("Ask a question")
    if not st.session_state.chunks:
        st.warning("The knowledge base is empty. You can still chat with the local model.")
    question = st.text_area("Your question", placeholder="Ask something about your documents…", height=100)
    top_k = st.slider("Retrieved passages", 1, 8, 4)
    if st.button("Generate answer", type="primary", disabled=not question.strip()):
        try:
            relevant = []
            if st.session_state.chunks:
                vectorizer, matrix = build_index(st.session_state.chunks)
                relevant = retrieve(question.strip(), st.session_state.chunks, vectorizer, matrix, top_k)
            context = "\n\n---\n\n".join(relevant) if relevant else "No relevant document passages were retrieved."
            system_prompt = (
                "You are RAGVoice, a helpful assistant. Answer clearly and honestly. Use provided "
                "document context when relevant. If the context does not contain the answer, say so. "
                "Do not invent citations or claim to have read documents absent from the context."
            )
            prompt = f"QUESTION:\n{question.strip()}\n\nDOCUMENT CONTEXT:\n{context}"
            with st.spinner("Generating with the local model…"):
                answer = ollama_chat(prompt, model_name.strip(), system_prompt, ollama_url)
            st.session_state.history.append({"question": question.strip(), "answer": answer, "context": relevant})
        except requests.exceptions.ConnectionError:
            st.error("Cannot connect to Ollama. Start Ollama and check the URL in the sidebar.")
        except requests.exceptions.HTTPError as exc:
            st.error(f"Ollama error: {exc}. Check that the selected model is installed.")
        except Exception as exc:
            st.error(f"Something went wrong: {exc}")
    for item in reversed(st.session_state.history):
        with st.container(border=True):
            st.markdown("**You asked**")
            st.write(item["question"])
            st.markdown("**RAGVoice**")
            st.write(item["answer"])
            if item["context"]:
                with st.expander("Retrieved context"):
                    for number, passage in enumerate(item["context"], 1):
                        st.markdown(f"**Passage {number}**")
                        st.write(passage)

with tab_voice:
    st.subheader("Speech input and output")
    st.write("Upload a recording to transcribe it locally with faster-whisper.")
    audio = st.file_uploader("Audio file", type=["wav", "mp3", "m4a", "ogg", "webm"], key="audio_input")
    if st.button("Transcribe audio", disabled=audio is None):
        try:
            with st.spinner("Transcribing locally…"):
                transcript = transcribe_audio(audio.getvalue(), Path(audio.name).suffix.lower())
            st.session_state.voice_transcript = transcript
            st.success("Transcription finished." if transcript else "No speech detected.")
        except ImportError:
            st.error("Install the dependencies from requirements.txt. The Whisper model may download on first use.")
        except Exception as exc:
            st.error(f"Transcription failed: {exc}")
    transcript = st.session_state.get("voice_transcript", "")
    if transcript:
        st.text_area("Transcription result", value=transcript, height=100)
        st.caption("Copy this transcription into the Ask RAGVoice tab to generate an answer.")
    st.divider()
    speech_text = st.text_area("Text to speak", placeholder="Enter text to read aloud…")
    if st.button("Speak text locally", disabled=not speech_text.strip()):
        try:
            speak_text(speech_text.strip())
            st.success("Speech playback finished.")
        except Exception as exc:
            st.error(f"Text-to-speech failed: {exc}")

st.divider()
st.caption("RAGVoice • Local-first AI • Uses a local Ollama endpoint, not a hosted LLM API.")
