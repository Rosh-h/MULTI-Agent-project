import os
import re
import json
import asyncio
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
# Ensure SynthesisAgent is imported if you decide to use it for cleaning data
from agents import SlackAgent, KnowledgeAgent, SearchAgent, CalendarAgent, CommunicationAgent

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

PLANNER_PROMPT_TEMPLATE = """
You are an expert planning agent. Create a JSON plan for: "{user_prompt}"
Available Agents & Format:
- "KnowledgeAgent": "What is [question]?" OR "Add knowledge: 'content' in filename"
- "SearchAgent": "Search for [query]"
- "SlackAgent": "Post \\"message\\" to #channel"
- "CalendarAgent": "Schedule [event name] for [time]"
- "CommunicationAgent": "Send SMS to [number]: [message]"

Sequence Rule: 
1. If the user asks a question, use SearchAgent first.
2. If SearchAgent is used, the system will automatically save it to KnowledgeAgent.
3. Use CommunicationAgent or SlackAgent only after info is gathered.

Respond with ONLY a JSON object: {{"steps": [{{"agent": "...", "action": "..."}}]}}
"""

class TaskOrchestrator:
    def __init__(self, task_id: str, prompt: str, ws_manager):
        self.task_id = task_id
        self.prompt = prompt
        self.ws_manager = ws_manager
        self.knowledge_agent = KnowledgeAgent()
        self.search_agent = SearchAgent()
        self.calendar_agent = CalendarAgent()
        self.communication_agent = CommunicationAgent()
        self.slack_agent = SlackAgent(token=os.getenv("SLACK_BOT_TOKEN"))
        self.plan = []

    async def _groq_request(self, user_prompt: str):
        if not GROQ_API_KEY:
            raise RuntimeError("Missing GROQ_API_KEY")

        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": PLANNER_PROMPT_TEMPLATE.format(user_prompt=user_prompt)}],
            "response_format": {"type": "json_object"},
            "temperature": 0.1
        }

        for attempt in range(3):
            r = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=60)
            if r.status_code == 429:
                await asyncio.sleep(5)
                continue
            r.raise_for_status()
            break
        
        result = r.json()["choices"][0]["message"]["content"]
        parsed = json.loads(result)
        return parsed.get("steps", parsed) if isinstance(parsed, dict) else parsed

    def _parse_calendar_action(self, action: str):
        title_match = re.search(r'schedule\s+(?:a\s+)?(.+?)\s+for', action, re.IGNORECASE)
        title = title_match.group(1).strip() if title_match else "Meeting"
        start_time = datetime.now() + timedelta(days=1) if "tomorrow" in action.lower() else datetime.now() + timedelta(minutes=30)
        start_time = start_time.replace(hour=10, minute=0)
        return {"title": title, "start_time": start_time.isoformat(), "end_time": (start_time + timedelta(hours=1)).isoformat()}

    async def execute_plan(self):
        try:
            await self.ws_manager.broadcast(json.dumps({"type": "log", "agent": "PlannerAgent", "message": "Groq is planning...", "log_type": "info"}))
            self.plan = await self._groq_request(self.prompt)
            if not isinstance(self.plan, list): self.plan = [self.plan]
        except Exception as e:
            await self.ws_manager.broadcast(json.dumps({"type": "log", "agent": "System", "message": f"Planning failed: {e}", "log_type": "error"}))
            return

        await self.ws_manager.broadcast(json.dumps({"type": "plan", "steps": self.plan}))
        
        context = "" # This stores the result of the previous step to pass to the next
        
        for step in self.plan:
            # We inject the 'context' (previous results) into the action if it's an action-based agent
            agent_name = step["agent"]
            action = step["action"]
            
            if context and agent_name in ["CommunicationAgent", "SlackAgent"]:
                action = f"{action}. Info: {context}"

            result = await self._execute_step(agent_name, action)
            context = result # Update context with the latest result
            
        await self.ws_manager.broadcast(json.dumps({"type": "log", "agent": "System", "message": "All tasks done!", "log_type": "success"}))

    async def _execute_step(self, agent, action):
        await self.ws_manager.broadcast(json.dumps({"type": "status_update", "step_action": action, "status": "in-progress"}))
        
        try:
            msg = ""
            if agent == "KnowledgeAgent":
                if "add knowledge" in action.lower():
                    match = re.search(r"knowledge:\s*['\"](.+?)['\"]\s*in\s+([^\s]+)", action, re.IGNORECASE)
                    msg = await self.knowledge_agent.add_knowledge(match.group(2), match.group(1)) if match else "Parse Error"
                else:
                    msg = await self.knowledge_agent.run(action)

            elif agent == "SearchAgent":
                msg = await self.search_agent.run(action)
                # --- AUTO-SAVE LOGIC ---
                # Whenever we search, we save the result to the knowledge base immediately
                filename = re.sub(r'[^a-zA-Z0-9]', '_', action[:20]) # Create filename from search query
                await self.knowledge_agent.add_knowledge(filename, msg)
                await self.ws_manager.broadcast(json.dumps({
                    "type": "log", 
                    "agent": "KnowledgeAgent", 
                    "message": f"Auto-saved results for '{action}' to knowledge base.", 
                    "log_type": "info"
                }))

            elif agent == "SlackAgent":
                msg = await self.slack_agent.execute(action)

            elif agent == "CommunicationAgent":
                msg = await self.communication_agent.run(action)

            elif agent == "CalendarAgent":
                msg = f"Event: {await self.calendar_agent.run(self._parse_calendar_action(action))}"
            
            else:
                msg = f"Task completed by {agent}"

            await self.ws_manager.broadcast(json.dumps({"type": "status_update", "step_action": action, "status": "completed"}))
            await self.ws_manager.broadcast(json.dumps({"type": "log", "agent": agent, "message": msg, "log_type": "info"}))
            return msg

        except Exception as e:
            await self.ws_manager.broadcast(json.dumps({"type": "log", "agent": agent, "message": f"Error: {e}", "log_type": "error"}))
            return f"Error: {str(e)}"