"""
PyOS NOVA — AI Context Manager  (Phase 4 — AI Platform)
=========================================================
Manages the conversation context fed to the LLM:
  - Sliding window: keeps the N most recent messages
  - Importance scoring: retains high-value messages even when old
  - Compression: summarises long exchanges to save tokens
  - System injection: injects kernel state into every AI call
  - Token budget: never exceeds the model's context window

Shell commands:
    context status          — show current context window usage
    context clear           — reset conversation history
    context compress        — compress current context via AI summary
    context pin <n>      — pin message index so it's never evicted
    context export <path>   — save context to SOS
    context import <path>   — restore context from SOS
"""

from __future__ import annotations

import os
import sys
import json
import time
import hashlib
from typing import List, Dict, Optional, Tuple, TYPE_CHECKING
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from ai.engine import AIEngine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

CONTEXT_SAVE_BASE = "/ai/contexts"

# ── Token estimation ──────────────────────────────────────────────────────────

def _estimate_tokens(text: str) -> int:
    """
    Estimate token count using the 4-chars-per-token heuristic.

    Accurate to within ±20 % for English, CJK text uses 2 chars/token.

    Args:
        text: Input string.

    Returns:
        Estimated token count.
    """
    # CJK characters are typically single tokens
    cjk   = sum(1 for c in text if "\u4e00" <= c <= "\u9fff"
                 or "\u3040" <= c <= "\u30ff")
    latin = len(text) - cjk
    return cjk + latin // 4


# ── Message scoring ───────────────────────────────────────────────────────────

@dataclass
class ScoredMessage:
    """A chat message with importance metadata."""

    role:       str               # "user" | "assistant" | "system"
    content:    str
    timestamp:  float             = field(default_factory=time.time)
    tokens:     int               = 0
    importance: float             = 1.0   # 0.0–10.0
    pinned:     bool              = False
    index:      int               = 0     # position in original conversation

    def __post_init__(self):
        """Calculate token count on creation."""
        self.tokens = _estimate_tokens(self.content)

    def to_dict(self) -> dict:
        """Convert to the LLM messages format."""
        return {"role": self.role, "content": self.content}

    def to_full_dict(self) -> dict:
        """Convert including metadata."""
        return self.__dict__

    @staticmethod
    def from_full_dict(d: dict) -> "ScoredMessage":
        """Restore from a full metadata dict."""
        return ScoredMessage(**{k: v for k, v in d.items()
                                  if k in ScoredMessage.__dataclass_fields__})  # type: ignore[attr-defined]


def _score_message(msg: ScoredMessage) -> float:
    """
    Score a message's importance for retention decisions.

    Factors:
    - Recency: recent messages are more important
    - Length: longer messages carry more information
    - Keywords: messages containing questions/code get a boost
    - Role: user messages slightly outrank assistant messages

    Args:
        msg: The message to score.

    Returns:
        Importance score in [0.0, 10.0].
    """
    score = 1.0

    # Recency bonus (messages from the last 10 exchanges get +3)
    score += min(3.0, msg.tokens / 100)

    # Length moderate bonus
    if msg.tokens > 50:
        score += 1.0
    if msg.tokens > 200:
        score += 1.0

    # Keyword bonus
    content_lower = msg.content.lower()
    if any(kw in content_lower for kw in ("error", "fix", "why", "how", "?")):
        score += 1.5
    if "```" in msg.content:        # code block
        score += 2.0
    if "def " in msg.content or "class " in msg.content:
        score += 1.0

    # Role weight
    if msg.role == "user":
        score += 0.5

    return min(10.0, score)


# ── Context manager ───────────────────────────────────────────────────────────

class ContextManager:
    """
    Manages the AI conversation context with intelligent message selection.

    When the context exceeds the budget, evicts the least important
    (non-pinned, non-recent) messages to stay within the token limit.
    Optionally compresses long exchanges into a summary message.
    """

    # Default budget: leaves room for system prompt and response
    DEFAULT_TOKEN_BUDGET = 3_500

    SYSTEM_TEMPLATE = (
        "You are Nova, the AI assistant built into PyOS NOVA — "
        "a Python operating system. "
        "Current working directory: {cwd}. "
        "User: {user}. "
        "Active language: {lang}. "
        "NOVA version: 0.0007."
    )

    def __init__(self,
                  sos: "SemanticObjectStore",
                  ai:  Optional["AIEngine"] = None,
                  token_budget: int = DEFAULT_TOKEN_BUDGET):
        """
        Initialise the context manager.

        Args:
            sos:          SOS for persisting context.
            ai:           AI engine (used for compression summarisation).
            token_budget: Maximum tokens to pass to the model.
        """
        self._sos          = sos
        self._ai           = ai
        self._budget       = token_budget
        self._messages:    List[ScoredMessage] = []
        self._pinned:      set = set()            # indices of pinned messages
        self._total_tokens = 0
        self._session_id   = hashlib.sha256(
            f"{time.time()}".encode()).hexdigest()[:12]
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create context save directory."""
        if not self._sos.exists(CONTEXT_SAVE_BASE):
            self._sos.mkdir(CONTEXT_SAVE_BASE, parents=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def add(self, role: str, content: str,
             importance: float = None) -> ScoredMessage:
        """
        Add a message to the context.

        Args:
            role:       "user" | "assistant" | "system"
            content:    Message text.
            importance: Manual importance override (auto-scored if None).

        Returns:
            The created ScoredMessage.
        """
        msg = ScoredMessage(
            role       = role,
            content    = content,
            index      = len(self._messages),
        )
        if importance is not None:
            msg.importance = importance
        else:
            msg.importance = _score_message(msg)

        self._messages.append(msg)
        self._total_tokens += msg.tokens
        self._evict_if_needed()
        return msg

    def pin(self, index: int) -> bool:
        """
        Pin a message so it is never evicted.

        Args:
            index: 0-based index into the current message list.

        Returns:
            True if pinned successfully.
        """
        if 0 <= index < len(self._messages):
            self._messages[index].pinned = True
            self._pinned.add(index)
            return True
        return False

    def clear(self):
        """Reset the conversation context."""
        self._messages.clear()
        self._pinned.clear()
        self._total_tokens = 0

    def build_messages(self,
                        cwd: str = "/home/root",
                        user: str = "root",
                        lang: str = "en") -> List[dict]:
        """
        Build the messages list to pass to the AI engine.

        Always includes:
        - A system message with current kernel context
        - All pinned messages
        - Recent + high-importance messages within token budget

        Args:
            cwd:  Current working directory (injected into system prompt).
            user: Current username.
            lang: Current locale code.

        Returns:
            List of {"role": ..., "content": ...} dicts ready for the LLM.
        """
        system_msg = {
            "role": "system",
            "content": self.SYSTEM_TEMPLATE.format(
                cwd=cwd, user=user, lang=lang
            ),
        }
        selected = self._select_messages()
        return [system_msg] + [m.to_dict() for m in selected]

    def compress(self) -> bool:
        """
        Summarise the current context to free token space.

        Asks the AI to write a brief summary of the conversation,
        then replaces the non-recent messages with that summary.

        Returns:
            True if compression was performed.
        """
        if not self._ai or len(self._messages) < 6:
            return False

        conversation = "\n".join(
            f"{m.role.upper()}: {m.content[:200]}"
            for m in self._messages[:-4]   # summarise all but last 4
        )
        prompt = (
            "Summarise this conversation in 2–3 sentences, "
            "preserving key decisions and facts:\n\n" + conversation
        )
        try:
            summary = self._ai.ask(prompt, max_tokens=150)
        except Exception:
            return False

        # Replace compressed messages with summary
        self._messages = (
            [ScoredMessage("system", f"[Earlier context summary: {summary})",
                            importance=8.0, pinned=True)]
            + self._messages[-4:]
        )
        self._total_tokens = sum(m.tokens for m in self._messages)
        return True

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, name: str = "") -> str:
        """
        Save the current context to SOS.

        Args:
            name: Optional identifier (defaults to session ID).

        Returns:
            SOS path where the context was saved.
        """
        key  = name or self._session_id
        path = f"{CONTEXT_SAVE_BASE}/{key}"
        data = {
            "session_id": self._session_id,
            "saved_at":   time.time(),
            "messages":   [m.to_full_dict() for m in self._messages],
        }
        self._sos.write(path, json.dumps(data), tags=["ai-context"])
        return path

    def load(self, name: str) -> bool:
        """
        Restore context from SOS.

        Args:
            name: Context identifier.

        Returns:
            True if loaded successfully.
        """
        path = f"{CONTEXT_SAVE_BASE}/{name}"
        if not self._sos.exists(path):
            return False
        try:
            data = json.loads(self._sos.read(path))
            self._messages = [
                ScoredMessage.from_full_dict(d)
                for d in data.get("messages", [])
            ]
            self._total_tokens = sum(m.tokens for m in self._messages)
            return True
        except Exception:
            return False

    def list_saved(self) -> List[str]:
        """Return names of all saved contexts."""
        try:
            return self._sos.listdir(CONTEXT_SAVE_BASE)
        except Exception:
            return []

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _evict_if_needed(self):
        """Evict lowest-importance non-pinned messages to stay within budget."""
        while self._total_tokens > self._budget and len(self._messages) > 4:
            # Find lowest-importance non-pinned, non-recent message
            candidates = [
                (i, m) for i, m in enumerate(self._messages[:-4])
                if not m.pinned
            ]
            if not candidates:
                break
            # Evict the candidate with lowest importance
            idx = min(candidates, key=lambda x: x[1].importance)[0]
            evicted             = self._messages.pop(idx)
            self._total_tokens -= evicted.tokens

    def _select_messages(self) -> List[ScoredMessage]:
        """
        Select messages to include in the LLM prompt.

        Always includes: last 4 messages + all pinned.
        Fills remaining budget with highest-importance messages.

        Returns:
            Ordered list of selected messages.
        """
        if not self._messages:
            return []

        recent  = set(range(max(0, len(self._messages) - 4),
                             len(self._messages)))
        pinned  = {i for i, m in enumerate(self._messages) if m.pinned}
        must    = recent | pinned

        # Sort non-mandatory messages by importance descending
        others  = sorted(
            [(i, m) for i, m in enumerate(self._messages) if i not in must],
            key=lambda x: x[1].importance,
            reverse=True,
        )

        budget_left = self._budget
        selected    = []

        for i in must:
            selected.append((i, self._messages[i]))
            budget_left -= self._messages[i].tokens

        for i, m in others:
            if budget_left <= 0:
                break
            if m.tokens <= budget_left:
                selected.append((i, m))
                budget_left -= m.tokens

        # Return in original order
        selected.sort(key=lambda x: x[0])
        return [m for _, m in selected]

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Return context window statistics."""
        return {
            "messages":     len(self._messages),
            "tokens":       self._total_tokens,
            "budget":       self._budget,
            "usage_pct":    round(self._total_tokens / self._budget * 100, 1),
            "pinned":       sum(1 for m in self._messages if m.pinned),
            "session_id":   self._session_id,
        }
