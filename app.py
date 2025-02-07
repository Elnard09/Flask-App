from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session, send_file
from googleapiclient.discovery import build
from youtube_transcript_api import YouTubeTranscriptApi
from PIL import Image
import openai
import uuid
import asyncio
import pytesseract
import requests
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
import os
import re
import logging
from datetime import datetime
from werkzeug.utils import secure_filename

load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///youtube_videos.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Your API Keys (make sure to keep them secure!)
openai.api_key = os.getenv("OPENAI_API_KEY")
youtube = build('youtube', 'v3', developerKey=os.getenv("YOUTUBE_API_KEY"))
app.secret_key = os.getenv("SECRET_KEY")

# Configuration for file uploads
UPLOAD_FOLDER = './uploads'
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'docx'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --------------------------------------------------------
# DATABASE MODELS
# --------------------------------------------------------

class YouTubeVideo(db.Model):
    """ Model to store YouTube video info. """
    id = db.Column(db.Integer, primary_key=True)
    video_id = db.Column(db.String(100), unique=True, nullable=False)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=False)
    transcript = db.Column(db.Text, nullable=False)

class ImageSummary(db.Model):
    """ Model to store image analysis summaries. """
    __tablename__ = 'image_summaries'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String, nullable=False)
    summary = db.Column(db.Text, nullable=False)

    def __repr__(self):
        return f"<ImageSummary session_id={self.session_id}>"

class FileSummary(db.Model):
    """ Model to store file-based content summaries. """
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('chat_session.id'), nullable=False)
    summary = db.Column(db.Text, nullable=False)

class CodeSummary(db.Model):
    """ Model to store code analysis. """
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('chat_session.id'), nullable=False)
    summary = db.Column(db.Text, nullable=False)

class ImageAnalysis(db.Model):
    """ Model to store analyzed image results. """
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('chat_session.id'), nullable=False)
    analysis = db.Column(db.Text, nullable=False)

class ChatSession(db.Model):
    """ Model representing a chat session (no user login references). """
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, nullable=False)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=False)
    video_id = db.Column(db.String(100), nullable=True)

    # Relationship to other summaries
    file_summary = db.relationship('FileSummary', backref='session', lazy=True)
    code_summary = db.relationship('CodeSummary', backref='chat_session', lazy=True)
    image_analysis = db.relationship('ImageAnalysis', backref='chat_session', lazy=True)

class ChatMessage(db.Model):
    """ Model to store messages in a chat session. """
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('chat_session.id'), nullable=False)
    message = db.Column(db.Text, nullable=False)
    is_user = db.Column(db.Boolean, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

# Create the database if it doesn't exist
with app.app_context():
    db.create_all()

# --------------------------------------------------------
# HELPER FUNCTIONS
# --------------------------------------------------------

def generate_image(prompt, size="1024x1024"):
    response = openai.Image.create(
        prompt=prompt,
        n=1,
        size=size
    )
    image_url = response['data'][0]['url']
    return image_url

def save_image_from_url(image_url, save_path):
    image_data = requests.get(image_url).content
    with open(save_path, 'wb') as image_file:
        image_file.write(image_data)

def create_chat_session(title, description, video_id=None):
    """Create a ChatSession in the database (no user references)."""
    chat_session = ChatSession(
        date=datetime.utcnow(),
        title=title,
        description=description,
        video_id=video_id
    )
    db.session.add(chat_session)
    db.session.commit()
    return chat_session.id

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_from_file(filepath):
    if filepath.endswith('.txt'):
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    elif filepath.endswith('.pdf'):
        from PyPDF2 import PdfReader
        reader = PdfReader(filepath)
        return ''.join([page.extract_text() for page in reader.pages])
    elif filepath.endswith('.docx'):
        import docx
        doc = docx.Document(filepath)
        return '\n'.join([paragraph.text for paragraph in doc.paragraphs])
    else:
        return None

def save_message(session_id, message, is_user):
    chat_message = ChatMessage(
        session_id=session_id,
        message=message,
        is_user=is_user
    )
    db.session.add(chat_message)
    db.session.commit()

def extract_video_id(youtube_link):
    """Extract the video ID from various YouTube URL formats."""
    video_id_match = re.search(
        r"(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})", 
        youtube_link
    )
    return video_id_match.group(1) if video_id_match else None

def get_video_info_and_transcript(video_id):
    """Fetch video info and transcript from YouTube."""
    video_response = youtube.videos().list(
        part='snippet',
        id=video_id
    ).execute()

    if not video_response['items']:
        raise ValueError("Video not found.")
    
    video_info = video_response['items'][0]['snippet']
    video_title = video_info['title']
    video_description = video_info['description']

    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        transcript_text = ' '.join([item['text'] for item in transcript_list])
    except Exception:
        transcript_text = "Transcript not available."

    return video_title, video_description, transcript_text

def save_video_to_db(video_id, title, description, transcript):
    """Save YouTube video information in the database."""
    video = YouTubeVideo(
        video_id=video_id, 
        title=title, 
        description=description, 
        transcript=transcript
    )
    db.session.add(video)
    db.session.commit()

def get_video_data(video_id):
    """Retrieve a video's title, description, and transcript from the database."""
    video = YouTubeVideo.query.filter_by(video_id=video_id).first()
    if video:
        return video.title, video.description, video.transcript
    return None

def get_openai_response(prompt, video_data, generate_summary=False):
    """Interact with OpenAI using a given prompt + video data."""
    video_title, video_description, video_transcript = video_data

    conversation_prompt = (
        f"Video Title: {video_title}\n"
        f"Description: {video_description}\n"
        f"Transcript: {video_transcript}\n\n"
        f"User: {prompt}\nAI:"
    )

    messages = [
        {
            "role": "system", 
            "content": "You are an AI assistant that answers questions based on the video content."
        },
        {"role": "user", "content": conversation_prompt}
    ]

    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=messages,
        temperature=0.7
    )

    ai_response = response['choices'][0]['message']['content']

    # Optionally generate a session title and description
    if generate_summary:
        summary_prompt = (
            "Based on this chat session, provide a brief title and description that summarize the main topics."
        )
        messages.append({"role": "user", "content": summary_prompt})
        
        summary_response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.7
        )
        title, description = summary_response['choices'][0]['message']['content'].split('\n', 1)
        return ai_response, title.strip(), description.strip()
    
    return ai_response

def get_dynamic_title_and_description(question, ai_response):
    """Generate simple title and description from question + AI response."""
    title = question if len(question) < 50 else question[:47] + "..."
    description = ai_response if len(ai_response) < 150 else ai_response[:147] + "..."
    return title, description

# --------------------------------------------------------
# FLASK ROUTES (NO LOGIN REQUIRED)
# --------------------------------------------------------

@app.route('/')
def home():
    """Render the main page (replaces former login page)."""
    return render_template('main.html')

@app.route('/main')
def main():
    return render_template('main.html')

@app.route('/chatAI')
def chatAI():
    return render_template('chatAI.html')

@app.route('/summarizer')
def summarizer():
    return render_template('summarizer.html')

@app.route('/history')
def history():
    """Display previously processed YouTube videos."""
    videos = YouTubeVideo.query.all()
    return render_template('history.html', videos=videos)

@app.route('/help')
def help():
    return render_template('help.html')

# --------------------------------------------------------
# YOUTUBE + VIDEO ROUTES
# --------------------------------------------------------

@app.route('/process_youtube_link', methods=['POST'])
def process_youtube_link():
    """Process a YouTube link to retrieve and store video info + transcript."""
    try:
        data = request.get_json()
        youtube_link = data['youtube_url']
        video_id = extract_video_id(youtube_link)

        if not video_id:
            return jsonify({'error': 'Invalid YouTube URL provided.'}), 400

        # Check if video is in the database
        video_data = get_video_data(video_id)
        if video_data:
            title, description, transcript = video_data
        else:
            # If not found, fetch from YouTube
            title, description, transcript = get_video_info_and_transcript(video_id)
            save_video_to_db(video_id, title, description, transcript)

        # Create a chat session with the video_id (no user references)
        session_id = create_chat_session(title=title, description=description, video_id=video_id)

        return jsonify({
            'message': 'Video processed successfully!',
            'transcript': transcript,
            'title': title,
            'description': description,
            'content_type': 'video',
            'session_id': session_id
        })

    except Exception as e:
        logging.error(f"Error processing YouTube link: {e}")
        return jsonify({'error': str(e)}), 400

@app.route('/ask_question', methods=['POST'])
def ask_question():
    """Ask a question about either code, file, video, or image content."""
    try:
        data = request.get_json()
        question = data.get('question')
        content_type = data.get('content_type')  # 'code', 'file', 'video', 'image'
        session_id = data.get('session_id')

        error_message = f"No analyzed {content_type} found in the session."

        if content_type == 'code':
            code_summary = CodeSummary.query.filter_by(session_id=session_id).first()
            if not code_summary:
                return jsonify({'error': error_message}), 400
            content_context = code_summary.summary

        elif content_type == 'file':
            file_summary = FileSummary.query.filter_by(session_id=session_id).first()
            if not file_summary:
                return jsonify({'error': error_message}), 400
            content_context = file_summary.summary

        elif content_type == 'video':
            chat_session = db.session.get(ChatSession, session_id)
            if not chat_session or not chat_session.video_id:
                return jsonify({'error': 'No associated video found for this session.'}), 400

            video_data = YouTubeVideo.query.filter_by(video_id=chat_session.video_id).first()
            if not video_data:
                return jsonify({'error': error_message}), 400
            
            # Combine title, description, and transcript
            content_context = (
                f"Title: {video_data.title}\n"
                f"Description: {video_data.description}\n"
                f"Transcript: {video_data.transcript}"
            )

        elif content_type == 'image':
            image_data = ImageSummary.query.filter_by(session_id=session_id).first()
            if not image_data:
                return jsonify({'error': error_message}), 400
            content_context = image_data.summary

        else:
            return jsonify({'error': 'Invalid content type provided.'}), 400

        # Generate AI response
        conversation_prompt = f"Content: {content_context}\nUser's question: {question}\nAI's answer:"

        response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are an AI assistant that answers questions based on the provided content."},
                {"role": "user", "content": conversation_prompt}
            ],
            temperature=0.7
        )

        ai_response = response['choices'][0]['message']['content']
        return jsonify({'response': ai_response, 'session_id': session_id})

    except Exception as e:
        logging.error(f"Error in ask_question: {e}")
        return jsonify({'error': str(e)}), 500

# --------------------------------------------------------
# FILE UPLOAD ROUTE
# --------------------------------------------------------

@app.route('/upload-file', methods=['POST'])
def upload_file():
    """Handle file uploads and extract text content from .txt, .pdf, .docx."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file part in the request'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        try:
            text_content = extract_text_from_file(filepath)
            if not text_content:
                return jsonify({'error': 'Failed to extract text from the file'}), 400

            # Create a chat session for file analysis
            session_id = create_chat_session(title="File Upload", description="File uploaded for analysis.")
            file_summary = FileSummary(session_id=session_id, summary=text_content)
            db.session.add(file_summary)
            db.session.commit()

            return jsonify({
                'summary': text_content,
                'content_type': 'file',
                'aiMessage': "You can now ask questions based on the full content of the file.",
                'session_id': session_id
            })

        except Exception as e:
            logging.error(f"Error processing file: {e}")
            return jsonify({'error': 'Failed to process the file.'}), 500
    else:
        return jsonify({'error': 'Invalid file type. Allowed types are txt, pdf, docx.'}), 400

# --------------------------------------------------------
# CODE SUMMARIZATION ROUTE
# --------------------------------------------------------

@app.route('/summarize-code', methods=['POST'])
def summarize_code():
    """Summarize and explain a given code block."""
    try:
        data = request.get_json()
        code_block = data.get('code')

        if not code_block:
            return jsonify({'error': 'Code block is required.'}), 400

        # Generate explanation using OpenAI
        code_prompt = (
            "Explain the following block of code in simple terms and summarize its purpose:\n\n"
            f"{code_block}\n\nExplanation and Summary:"
        )

        messages = [
            {"role": "system", "content": "You are an AI assistant that explains code to users in simple terms."},
            {"role": "user", "content": code_prompt}
        ]

        response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.7
        )

        explanation = response['choices'][0]['message']['content']

        # Create a chat session to store the entire code
        session_id = create_chat_session(title="Code Analysis", description="Code uploaded and analyzed.")
        code_summary = CodeSummary(session_id=session_id, summary=code_block)
        db.session.add(code_summary)
        db.session.commit()

        return jsonify({
            'explanation': explanation,
            'content_type': 'code',
            'aiMessage': 'You can now ask questions based on the analyzed code.',
            'session_id': session_id
        })
    except Exception as e:
        logging.error(f"Error summarizing code: {e}")
        return jsonify({'error': 'Failed to summarize the code.'}), 500

# --------------------------------------------------------
# IMAGE ANALYSIS ROUTE
# --------------------------------------------------------

@app.route('/analyze-image', methods=['POST'])
def analyze_image():
    """Analyze an image by extracting text via Tesseract and generating an AI-based analysis."""
    if 'image' not in request.files:
        return jsonify({'error': 'No image uploaded.'}), 400

    image = request.files['image']
    if image.filename == '':
        return jsonify({'error': 'No file selected.'}), 400

    try:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(image.filename))
        image.save(filepath)

        # Extract text with Tesseract
        extracted_text = pytesseract.image_to_string(Image.open(filepath))

        # Generate analysis using OpenAI
        analysis_prompt = (
            f"Analyze the following text extracted from an image:\n"
            f"{extracted_text}\n\nAnalysis:"
        )
        messages = [
            {"role": "system", "content": "You are an AI that analyzes images and provides insights."},
            {"role": "user", "content": analysis_prompt}
        ]

        response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.7
        )
        analysis = response['choices'][0]['message']['content']

        return jsonify({'analysis': analysis, 'content_type': 'image'})
    except Exception as e:
        logging.error(f"Error analyzing image: {e}")
        return jsonify({'error': 'Failed to analyze the image.'}), 500

# --------------------------------------------------------
# CHAT SESSION ROUTES (CREATE, VIEW, DELETE)
# --------------------------------------------------------

@app.route('/save-chat-session', methods=['POST'])
def save_chat_session():
    """Explicitly save a new chat session with given date, title, description."""
    data = request.get_json()
    date_str = data['date']
    title = data['title']
    description = data['description']

    # Convert date_str to a datetime object
    try:
        date_obj = datetime.strptime(date_str, '%m/%d/%Y, %I:%M:%S %p')
    except ValueError as e:
        return jsonify({"error": f"Invalid date format: {e}"}), 400

    chat_session = ChatSession(
        date=date_obj,
        title=title,
        description=description
    )
    db.session.add(chat_session)
    db.session.commit()

    return jsonify({"success": True})

@app.route('/chat-sessions', methods=['GET'])
def get_chat_sessions():
    """Retrieve all chat sessions."""
    sessions = ChatSession.query.all()
    return jsonify([
        {
            'date': session.date.strftime('%Y-%m-%d %H:%M:%S'),
            'title': session.title,
            'description': session.description
        } 
        for session in sessions
    ])

@app.route('/delete-chat-session/<int:session_id>', methods=['DELETE'])
def delete_chat_session(session_id):
    """Delete a specific chat session by ID."""
    try:
        chat_session = db.session.get(ChatSession, session_id)
        if chat_session:
            db.session.delete(chat_session)
            db.session.commit()
            return jsonify({'success': True})
        else:
            return jsonify({'error': 'Session not found'}), 404
    except Exception as e:
        logging.error(f"Error deleting session: {e}")
        return jsonify({'error': 'An error occurred while deleting the session.'}), 500

@app.route('/chat-session/<int:session_id>', methods=['GET'])
def get_chat_session(session_id):
    """Retrieve a specific chat session and its messages."""
    try:
        session_obj = ChatSession.query.get_or_404(session_id)
        messages = ChatMessage.query.filter_by(session_id=session_id).order_by(ChatMessage.timestamp).all()
        return jsonify({
            'id': session_obj.id,
            'title': session_obj.title,
            'description': session_obj.description,
            'messages': [
                {
                    'message': msg.message,
                    'is_user': msg.is_user,
                    'timestamp': msg.timestamp.strftime('%Y-%m-%d %H:%M:%S')
                } 
                for msg in messages
            ]
        })
    except Exception as e:
        logging.error(f"Error fetching session: {e}")
        return jsonify({'error': 'Failed to retrieve session messages.'}), 500

@app.route('/get-chat-history', methods=['GET'])
def get_chat_history():
    """Return all chat sessions in descending order of creation date."""
    sessions = ChatSession.query.order_by(ChatSession.date.desc()).all()
    session_data = [
        {
            'id': s.id,
            'title': s.title,
            'description': s.description,
            'date': s.date.strftime('%Y-%m-%d %H:%M:%S')
        }
        for s in sessions
    ]
    return jsonify(session_data)

@app.route('/chat-session/view/<int:session_id>', methods=['GET'])
def view_chat_session(session_id):
    """Render a page showing messages from one chat session."""
    session_obj = ChatSession.query.get_or_404(session_id)
    messages = ChatMessage.query.filter_by(session_id=session_id).order_by(ChatMessage.timestamp).all()
    return render_template(
        'view_chat.html',
        title=session_obj.title,
        description=session_obj.description,
        messages=[
            {
                'message': msg.message,
                'is_user': msg.is_user,
                'timestamp': msg.timestamp.strftime('%Y-%m-%d %H:%M:%S')
            }
            for msg in messages
        ]
    )

# --------------------------------------------------------
# ERROR HANDLERS
# --------------------------------------------------------

@app.errorhandler(404)
def page_not_found(e):
    return jsonify({'error': 'Resource not found'}), 404

@app.errorhandler(500)
def server_error(e):
    return jsonify({'error': 'Internal server error'}), 500

# --------------------------------------------------------
# MAIN
# --------------------------------------------------------

if __name__ == '__main__':
    app.run(debug=True)
