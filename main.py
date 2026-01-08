from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import json
import os
from dotenv import load_dotenv

# Load environment variables from .env file
# ... imports ...

load_dotenv()

import agents 
import orchestrator 

# Sync the keys correctly
orchestrator.GROQ_API_KEY = os.getenv('GROQ_API_KEY', '')
agents.GROQ_API_KEY = os.getenv('GROQ_API_KEY', '')
agents.SLACK_BOT_TOKEN = os.getenv('SLACK_BOT_TOKEN', '')
agents.TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID', '')
agents.TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN', '')
agents.TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER', '')

if not orchestrator.GROQ_API_KEY:
    print("FATAL ERROR: GROQ_API_KEY not found in .env")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
    async def broadcast(self, message: str):
        for connection in self.active_connections:
            await connection.send_text(message)

manager = ConnectionManager()

class TaskRequest(BaseModel):
    prompt: str

@app.post("/api/tasks")
async def create_task(task_request: TaskRequest):
    print(f"Received task: {task_request.prompt}")
    task_id = "task_12345"
    orch_instance = orchestrator.TaskOrchestrator(task_id, task_request.prompt, manager)
    asyncio.create_task(orch_instance.execute_plan())
    return {"status": "Task received", "task_id": task_id}

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await manager.connect(websocket)
    print("WebSocket connection successful.")
    try:
        while True:
            await websocket.receive_text() # Keep connection alive
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        print("WebSocket disconnected.")

# This will serve your index.html file from the new 'static' folder
app.mount("/", StaticFiles(directory="static", html=True), name="static")

@app.get("/")
async def read_root():
    return FileResponse('static/index.html')

