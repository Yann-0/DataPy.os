"""
PyOS NOVA — AI Memory Layer
==============================
The AI remembers: your name, preferences, past commands, files you care about,
errors you've made, and context about your projects.

Memory is stored as SOS objects tagged #ai-memory.
On every LLM call, the top-5 most relevant memories are injected
into the system prompt as context.

Commands:
  memory list              — show all memories
  memory add <text>        — manually add a memory
  memory forget <id>       — delete a memory
  memory search <query>    — search memories
  memory clear             — wipe all memories
"""

import os, sys, time, json, hashlib, re
from typing import List, Dict, Optional, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from ai.engine import AIEngine

MEMORY_TAG  = "ai-memory"
MEMORY_BASE = "/ai/memory"


class Memory:
    """Memory."""
    def __init__(self, mid: str, text: str, kind: str,
                 importance: float, ts: float, access_count: int = 0):
        """Initialise the instance."""
        self.mid          = mid
        self.text         = text
        self.kind         = kind          # fact|preference|error|project|command
        self.importance   = importance    # 0.0 – 1.0
        self.ts           = ts
        self.access_count = access_count

    def age_days(self) -> float:
        """Age days.


            Returns:
                float: Result.
            """
        return (time.time() - self.ts) / 86400

    def relevance_score(self, query: str) -> float:
        """Naive relevance: keyword overlap + recency + importance."""
        terms    = set(query.lower().split())
        overlap  = sum(1 for t in terms if t in self.text.lower())
        recency  = max(0, 1 - self.age_days()/30)
        return (overlap * 0.5 + recency * 0.2 + self.importance * 0.3)

    def to_dict(self) -> dict:
        """To dict.


            Returns:
                dict: Result.
            """
        return {"mid": self.mid, "text": self.text, "kind": self.kind,
                "importance": self.importance, "ts": self.ts,
                "access_count": self.access_count}

    @staticmethod
    def from_dict(d: dict) -> "Memory":
        """From dict.

            Args:
            d (dict): D.


            Returns:
                'Memory': Result.
            """
        return Memory(d["mid"], d["text"], d.get("kind","fact"),
                      d.get("importance",0.5), d.get("ts",time.time()),
                      d.get("access_count",0))


class MemoryManager:
    """
    Manages the AI's persistent memory store.
    Memories are SOS objects at /ai/memory/<mid>.
    """

    MAX_MEMORIES = 500   # keep the best N when pruning

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the instance."""
        self.sos = sos
        self._ensure_dir()

    def _ensure_dir(self):
        """Ensure dir."""
        if not self.sos.exists(MEMORY_BASE):
            self.sos.mkdir(MEMORY_BASE, parents=True)

    def _path(self, mid: str) -> str:
        """Path.

            Args:
            mid (str): Mid.


            Returns:
                str: Result.
            """
        return f"{MEMORY_BASE}/{mid}"

    def add(self, text: str, kind: str = "fact",
            importance: float = 0.5) -> Memory:
        """Add the operation.

            Args:
            text (str): Text.
            kind (str): Kind, defaults to 'fact'.
            importance (float): Importance, defaults to 0.5.


            Returns:
                Memory: Result.
            """
        mid = hashlib.sha256(f"{text}{time.time()}".encode()).hexdigest()[:12]
        mem = Memory(mid, text, kind, importance, time.time())
        self.sos.write(self._path(mid), json.dumps(mem.to_dict()),
                       tags=[MEMORY_TAG, kind])
        return mem

    def get(self, mid: str) -> Optional[Memory]:
        """Return the the operation.

            Args:
            mid (str): Mid.


            Returns:
                Optional[Memory]: Result.
            """
        path = self._path(mid)
        try:
            data = json.loads(self.sos.read(path))
            return Memory.from_dict(data)
        except Exception:
            return None

    def all(self) -> List[Memory]:
        """All.


            Returns:
                List[Memory]: Result.
            """
        mems = []
        for name in self.sos.listdir(MEMORY_BASE):
            m = self.get(name)
            if m:
                mems.append(m)
        return sorted(mems, key=lambda m: m.ts, reverse=True)

    def forget(self, mid: str):
        """Forget.

            Args:
            mid (str): Mid.
            """
        try:
            self.sos.remove(self._path(mid))
        except Exception:
            pass

    def clear(self):
        """Clear the operation."""
        for name in self.sos.listdir(MEMORY_BASE):
            try:
                self.sos.remove(self._path(name))
            except Exception:
                pass

    def search(self, query: str, n: int = 5) -> List[Memory]:
        """Search.

            Args:
            query (str): Query.
            n (int): N, defaults to 5.


            Returns:
                List[Memory]: Result.
            """
        mems  = self.all()
        scored = sorted(mems, key=lambda m: m.relevance_score(query), reverse=True)
        # Increment access count for returned memories
        for m in scored[:n]:
            m.access_count += 1
            try:
                self.sos.write(self._path(m.mid), json.dumps(m.to_dict()),
                               tags=[MEMORY_TAG, m.kind])
            except Exception:
                pass
        return scored[:n]

    def build_context(self, query: str) -> str:
        """
        Build a context string to inject into LLM system prompt.
        Returns empty string if no relevant memories.
        """
        relevant = self.search(query, n=5)
        if not relevant:
            return ""
        lines = ["Relevant context from memory:"]
        for m in relevant:
            age = f"{m.age_days():.0f}d ago" if m.age_days() > 1 else "recently"
            lines.append(f"  [{m.kind}, {age}] {m.text}")
        return "\n".join(lines)

    def auto_learn(self, user_input: str, ai_response: str):
        """
        Automatically extract and store useful memories from a conversation turn.
        Heuristic-based — looks for patterns that are worth remembering.
        """
        # Learn user name
        m = re.search(r"(?:i(?:'m| am)|my name is|call me)\s+(\w+)", user_input, re.I)
        if m:
            name = m.group(1)
            if not any(name.lower() in mem.text.lower()
                       for mem in self.search("user name", n=5)):
                self.add(f"User's name is {name}", kind="fact", importance=0.9)

        # Learn preferences
        pref_patterns = [
            r"i (?:prefer|like|love|hate|dislike|always use|never use)\s+(.+)",
            r"i (?:want|need) you to (?:always|never)\s+(.+)",
        ]
        for pat in pref_patterns:
            m = re.search(pat, user_input, re.I)
            if m:
                self.add(f"User preference: {m.group(0)}", kind="preference",
                         importance=0.8)

        # Learn project info
        if any(w in user_input.lower() for w in ["working on","building","project","my app"]):
            self.add(f"User said: {user_input[:100]}", kind="project", importance=0.6)

        # Prune if over limit
        if len(self.all()) > self.MAX_MEMORIES:
            self._prune()

    def _prune(self):
        """Keep the most important and recently-accessed memories."""
        mems = self.all()
        score = lambda m: m.importance * 0.4 + m.access_count * 0.3 + (1/max(m.age_days(),1)) * 0.3
        mems.sort(key=score, reverse=True)
        for m in mems[self.MAX_MEMORIES:]:
            self.forget(m.mid)

    def stats(self) -> dict:
        """Return usage statistics.


            Returns:
                dict: Result.
            """
        mems = self.all()
        kinds = {}
        for m in mems:
            kinds[m.kind] = kinds.get(m.kind, 0) + 1
        return {"total": len(mems), "by_kind": kinds}


# ── Patched AIEngine that injects memory context ───────────────────────────
def patch_engine_with_memory(engine, memory_manager: MemoryManager):
    """
    Monkey-patch the AIEngine to inject memory context into every call.
    Call once after kernel boot.
    """
    orig_complete = engine.complete

    def _complete_with_memory(prompt, system_key="assistant", **kw):
        """Complete with memory.

            Args:
            prompt: Prompt.
            system_key: System key, defaults to 'assistant'.
            """
        ctx = memory_manager.build_context(prompt)
        if ctx:
            augmented = f"{ctx}\n\n---\n\n{prompt}"
        else:
            augmented = prompt
        yield from orig_complete(augmented, system_key=system_key, **kw)

    orig_chat = engine.chat

    def _chat_with_memory(messages, system_key="assistant", **kw):
        """Chat with memory.

            Args:
            messages: Messages.
            system_key: System key, defaults to 'assistant'.
            """
        if messages:
            last = messages[-1].get("content","")
            ctx  = memory_manager.build_context(last)
            if ctx:
                messages = list(messages)
                messages[-1] = dict(messages[-1])
                messages[-1]["content"] = f"{ctx}\n\n---\n\n{last}"
        yield from orig_chat(messages, system_key=system_key, **kw)

    engine.complete  = _complete_with_memory
    engine.chat      = _chat_with_memory
    engine._memory   = memory_manager
    return engine
