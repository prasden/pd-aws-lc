import json
import threading
from pathlib import Path
from typing import Any

from strands import ToolContext, tool
from strands.agent import AgentResult
from strands.tools.executors import SequentialToolExecutor
from strands.types.agent import Limits
from strands_harness import create_harness

from .sandbox import Sandbox, VerifyResult

_AI_CONFIG = json.loads((Path(__file__).parents[4] / ".github/workflows/ai-config.json").read_text())
_MODEL = f"bedrock/{_AI_CONFIG['opus']}"
_LIMITS = Limits(turns=150, total_tokens=6_000_000)
_TIMEOUT = 45 * 60


class Agent:
    """A Strands harness agent with no plugins, sessions, memory, skills, or tools unless options add them."""

    def __init__(self, instructions: str, **options: Any):
        self._agent = create_harness(**{
            "model": _MODEL,
            "instructions": instructions,
            "builtin_tools": [],
            "builtin_plugins": [],
            "context_manager": "auto",
            "session": False,
            "memory": False,
            "skills": False,
            "background_tasks": False,
            "tool_executor": SequentialToolExecutor(),
        } | options)

    def __call__(self, message: str, **kwargs: Any) -> AgentResult:
        """Run the agent within its turn and token limits, cancelling it when the timeout passes."""
        deadline = threading.Event()
        timer = threading.Timer(_TIMEOUT, deadline.set)
        timer.start()
        try:
            return self._agent(message, limits=_LIMITS, cancel_signal=deadline, **kwargs)
        finally:
            timer.cancel()


@tool(context=True)
def run_integration(tool_context: ToolContext) -> VerifyResult:
    """Replay every failed CI job on a fresh copy of aws-lc with your current changes and return each exit code."""
    sandbox: Sandbox = tool_context.invocation_state["sandbox"]
    return sandbox.replay(sandbox.awslc.diff(sandbox.editable))
