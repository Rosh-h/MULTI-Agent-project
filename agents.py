import os
import re
import json
import asyncio
import requests
from slack_sdk.web.async_client import AsyncWebClient
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from twilio.rest import Client
from duckduckgo_search import DDGS
from dotenv import load_dotenv

load_dotenv()

# Configuration
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(BASE_DIR, 'credentials.json')
TOKEN_PATH = os.path.join(BASE_DIR, 'token.json')

class SlackAgent:
    def __init__(self, token: str):
        self.client = AsyncWebClient(token=token)

    # Renamed from 'run' to 'execute' to fix your error
    async def execute(self, action: str):
        # Improved regex to handle various quoting styles
        m = re.search(r'Post\s+["\'](.+?)["\']\s+to\s+(#[^\s]+)', action, re.IGNORECASE)
        if m:
            msg, channel = m.groups()
            try:
                await self.client.chat_postMessage(channel=channel, text=msg)
                return f"Message successfully posted to {channel}"
            except Exception as e:
                return f"Slack Error: {str(e)}"
        return "Slack failed: Ensure format is 'Post \"message\" to #channel'"

class KnowledgeAgent:
    def __init__(self, directory="knowledge_base"):
        self.directory = os.path.join(BASE_DIR, directory)
        os.makedirs(self.directory, exist_ok=True)
        self.knowledge = self._load_knowledge()

    def _load_knowledge(self):
        full_text = ""
        try:
            if os.path.exists(self.directory):
                for filename in os.listdir(self.directory):
                    if filename.endswith(".txt"):
                        with open(os.path.join(self.directory, filename), 'r', encoding="utf-8") as f:
                            full_text += f.read() + "\n"
        except Exception as e:
            print(f"Error loading knowledge: {e}")
        return full_text

    # ADDED: This fixes the 'no attribute add_knowledge' error
    async def add_knowledge(self, filename: str, content: str) -> str:
        # Sanitize filename
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', filename)
        file_path = os.path.join(self.directory, safe_name + ".txt")
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content.strip())
            # Reload knowledge so the next question can use this new info
            self.knowledge = self._load_knowledge()
            return f"Knowledge successfully stored in {safe_name}.txt"
        except Exception as e:
            return f"Error saving knowledge: {str(e)}"

    async def run(self, query: str) -> str:
        if not self.knowledge:
            return "Knowledge base is empty. I don't have internal info on this."
        
        prompt = f"Context: {self.knowledge}\n\nQuestion: {query}\n\nAnswer only based on context:"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}]
        }
        
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
            r.raise_for_status()
            return r.json()['choices'][0]['message']['content'].strip()
        except Exception as e:
            return f"Knowledge retrieval error: {str(e)}"

class SearchAgent:
    async def run(self, query: str) -> str:
        # CLEANING: Remove "Search for" or "SearchAgent" if the orchestrator passes it
        clean_query = re.sub(r'^(Search for|SearchAgent|find|tell me about)\s+', '', query, flags=re.IGNORECASE).strip()
        
        try:
            with DDGS() as ddgs:
                results = [r for r in ddgs.text(clean_query, max_results=3)]
            if not results:
                return f"No search results found on the web for '{clean_query}'."
            return "\n".join([f"{r['title']}: {r['body']}" for r in results])
        except Exception as e:
            return f"Search error: {e}"

class CalendarAgent:
    def __init__(self):
        self.scopes = ["https://www.googleapis.com/auth/calendar"]

    async def run(self, event_details: dict):
        # Note: Changed parameter to dict to match orchestrator's _parse_calendar_action
        creds = None
        if os.path.exists(TOKEN_PATH):
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, self.scopes)
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(CREDENTIALS_PATH):
                    return "Error: credentials.json not found."
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, self.scopes)
                creds = flow.run_local_server(port=0)
            with open(TOKEN_PATH, "w") as token:
                token.write(creds.to_json())

        try:
            service = build("calendar", "v3", credentials=creds)
            event = {
                "summary": event_details.get("title", "AI Task"),
                "start": {"dateTime": event_details.get("start_time"), "timeZone": "Asia/Kolkata"},
                "end": {"dateTime": event_details.get("end_time"), "timeZone": "Asia/Kolkata"}
            }
            created_event = service.events().insert(calendarId="primary", body=event).execute()
            return f"Event created: {created_event.get('htmlLink')}"
        except Exception as e:
            return f"Calendar Error: {str(e)}"

class CommunicationAgent:
    def __init__(self):
        self.client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None

    async def run(self, action: str):
        # Matches phone number anywhere in the string
        phone_match = re.search(r'(\+?\d[\d\s-]{9,15})', action)
        
        if phone_match and self.client:
            to_no = phone_match.group(1).replace(" ", "").replace("-", "")
            if not to_no.startswith('+'): to_no = "+" + to_no
            
            # Message is the part after the phone number
            msg_content = action.split(phone_match.group(1))[-1].strip(": ")

            try:
                message = self.client.messages.create(
                    body=msg_content[:160], 
                    from_=TWILIO_PHONE_NUMBER, 
                    to=to_no
                )
                return f"SMS sent! SID: {message.sid}"
            except Exception as e:
                return f"Twilio Error: {str(e)}"
        
        return "Communication failed: Ensure phone number is valid."