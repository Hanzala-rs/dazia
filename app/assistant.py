"""Orchestrator: message -> LLM proposes one structured action -> backend validates -> tool -> reply.

Flow (easy to trace):
  handle(message)
    1. If a reminder is waiting for a clarified time, try to finish it without the LLM.
    2. Ask the LLM for ONE action as JSON: {"tool", "args", "reply"}.
    3. Look the tool up in the allowlisted TOOLS registry and validate args with Pydantic.
    4. Run the tool with a fresh DB connection; the reply text comes from the tool, not the model.
"""
import logging
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError

from app import db
from app.llm import LLMClient, LLMError, LLMUnavailable
from app.tools import ToolResult
from app.tools import reminders

log = logging.getLogger(__name__)

# Allowlist: the only tools the model may trigger. name -> (argument model, function).
TOOLS: dict[str, tuple[type[BaseModel], Callable]] = {
    "create_reminder": (reminders.CreateReminderArgs, reminders.create_reminder),
    "list_reminders": (reminders.ListRemindersArgs, reminders.list_reminders),
    "complete_reminder": (reminders.CompleteReminderArgs, reminders.complete_reminder),
    "snooze_reminder": (reminders.SnoozeReminderArgs, reminders.snooze_reminder),
}
DATETIME_TOOL = "get_current_datetime"  # answered in Python, never by the model
NO_TOOL = "none"

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "enum": [*TOOLS, DATETIME_TOOL, NO_TOOL]},
        "args": {"type": "object"},
        "reply": {"type": "string"},
    },
    "required": ["tool", "args", "reply"],
}

HISTORY_MESSAGES = 6  # recent user/assistant messages sent to the LLM
FOLLOWUP_MAX_WORDS = 4  # longer replies to a clarification are treated as a new request

SYSTEM_PROMPT = """You are DAZIA, a concise personal assistant. Now: {now} ({weekday}), time zone {tz}.
Reply with ONE JSON action: {{"tool": ..., "args": {{...}}, "reply": "..."}}.
Tools:
- create_reminder: args {{"title": str, "when": str}}. Put the user's time phrase in "when" EXACTLY as written (e.g. "tomorrow at 9 AM"). Never compute or convert dates yourself.
- list_reminders: args {{"range": "today"|"tomorrow"|"upcoming"|"all"}}.
- complete_reminder: args {{"reminder_id": int}} (the reminder's number).
- snooze_reminder: args {{"reminder_id": int, "when": str}}. Move a reminder to a new time; put the time phrase in "when" EXACTLY as written (e.g. "in 10 minutes").
- get_current_datetime: args {{}}. For questions about the current date or time.
- none: args {{}}. Chat or questions; put your answer in "reply".
For tools other than none, keep "reply" short; the system reports the real result."""


class AssistantReply(BaseModel):
    reply: str
    tool: str | None = None  # name of the tool that ran, if any
    needs_clarification: bool = False
    data: dict = Field(default_factory=dict)


class Assistant:
    def __init__(self, llm: LLMClient, db_path: str, tz: str, clock: Callable[[], datetime] | None = None):
        """`clock` returns an aware 'now' (injectable for tests)."""
        self.llm = llm
        self.db_path = db_path
        self.tz = tz
        self._zone = ZoneInfo(tz)
        self.clock = clock or (lambda: datetime.now(self._zone))
        self.history: list[dict] = []  # [{"role": "user"|"assistant", "content": str}]
        self.pending: dict | None = None  # {"title", "when"} of a reminder awaiting a clearer time

    # ---- public entry point -------------------------------------------------
    def handle(self, message: str) -> AssistantReply:
        message = message.strip()
        result = self._try_resolve_pending(message)
        if result is None:
            result = self._handle_with_llm(message)
        self._remember(message, result.reply)
        return result

    # ---- steps --------------------------------------------------------------
    def _try_resolve_pending(self, message: str) -> AssistantReply | None:
        """Finish a reminder that needed a clearer time (e.g. "AM") without calling the LLM.
        Returns None when the message should be processed normally instead."""
        pending, self.pending = self.pending, None  # one shot: always cleared here
        if pending is None or len(message.split()) > FOLLOWUP_MAX_WORDS:
            return None
        try:
            args = reminders.CreateReminderArgs(title=pending["title"], when=f"{pending['when']} {message}")
        except ValidationError:
            return None
        reply = self._run_tool("create_reminder", args)
        if reply.needs_clarification:
            log.info("Clarification follow-up did not resolve; asking the LLM instead")
            return None
        return reply

    def _handle_with_llm(self, message: str) -> AssistantReply:
        messages = [{"role": "system", "content": self._system_prompt()}, *self.history]
        messages.append({"role": "user", "content": message})
        try:
            action = self.llm.complete_json(messages, ACTION_SCHEMA)
        except LLMUnavailable as exc:
            log.warning("LLM unavailable: %s", exc)
            return AssistantReply(reply=f"I can't reach my language model right now. {exc}")
        except LLMError as exc:
            log.warning("LLM error: %s", exc)
            return AssistantReply(reply=f"Sorry, I had trouble understanding that. Please try again. ({exc})")

        # Model output is untrusted: check its shape before using it.
        tool = action.get("tool") if isinstance(action, dict) else None
        args = action.get("args") if isinstance(action, dict) else None
        model_reply = action.get("reply") if isinstance(action, dict) else None
        if not isinstance(args, dict):
            args = {}
        if not isinstance(model_reply, str):
            model_reply = ""
        log.info("LLM proposed tool=%s", tool)

        if tool == NO_TOOL:
            return AssistantReply(reply=model_reply.strip() or "Sorry, I'm not sure how to help with that.")
        if tool == DATETIME_TOOL:
            return self._current_datetime()
        if tool not in TOOLS:
            log.warning("Rejected unknown tool from LLM: %r", tool)
            return AssistantReply(reply="Sorry, I can't do that. I can create, list, complete, and snooze reminders.")

        args_model, _ = TOOLS[tool]
        try:
            validated = args_model.model_validate(args)
        except ValidationError as exc:
            log.info("Invalid args for %s: %d error(s)", tool, exc.error_count())
            return AssistantReply(reply=_clarify(tool), tool=None, needs_clarification=True)

        reply = self._run_tool(tool, validated)
        if tool == "create_reminder" and reply.needs_clarification:
            self.pending = {"title": validated.title, "when": validated.when}
        return reply

    def _run_tool(self, name: str, args: BaseModel) -> AssistantReply:
        """Run one allowlisted tool with its own short-lived DB connection."""
        _, func = TOOLS[name]
        conn = db.connect(self.db_path)
        try:
            result: ToolResult = func(conn, args, self.clock(), self.tz)
        except Exception:
            log.exception("Tool %s failed", name)
            return AssistantReply(reply="Sorry, something went wrong while doing that.", tool=name)
        finally:
            conn.close()
        log.info("Tool %s ok=%s clarify=%s", name, result.ok, result.needs_clarification)
        return AssistantReply(
            reply=result.message, tool=name, needs_clarification=result.needs_clarification, data=result.data
        )

    def _current_datetime(self) -> AssistantReply:
        now = self.clock().astimezone(self._zone)
        text = f"It's {now:%A, %d %B %Y}, {now:%I:%M %p} ({self.tz})."
        return AssistantReply(reply=text, tool=DATETIME_TOOL, data={"now": now.isoformat()})

    # ---- helpers ------------------------------------------------------------
    def _system_prompt(self) -> str:
        now = self.clock().astimezone(self._zone)
        return SYSTEM_PROMPT.format(now=now.strftime("%Y-%m-%d %H:%M"), weekday=now.strftime("%A"), tz=self.tz)

    def _remember(self, user_text: str, reply_text: str) -> None:
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": reply_text})
        del self.history[:-HISTORY_MESSAGES]


def _clarify(tool: str) -> str:
    if tool == "create_reminder":
        return "What should I remind you about, and when? For example: 'remind me to call Ali tomorrow at 9 AM'."
    if tool == "complete_reminder":
        return "Which reminder should I mark as done? Please give its number."
    if tool == "snooze_reminder":
        return "Which reminder should I snooze, and until when? For example: 'snooze reminder 2 to 6 PM'."
    return "Which reminders would you like to see: today, tomorrow, upcoming, or all?"
