"""
PyOS NOVA — Automatic Data Classification
==========================================
AI + regex scan objects on write and classify them automatically:

  PUBLIC       — safe to share openly
  INTERNAL     — organisation-internal, not for outside
  CONFIDENTIAL — sensitive, encryption recommended
  SECRET       — highly sensitive, encryption required, 2FA to read

Classification is stored as a tag on the object.
Confidential → auto-encrypts if crypto engine is available.
Secret → auto-encrypts + requires capability token to read.

Shell commands:
  classify <path>        — force reclassify an object
  classify show <path>   — show classification
  classify policy        — show classification policy
  classify stats         — statistics by classification level
"""

from __future__ import annotations
import os, sys, re, time
from typing import Optional, Dict, List, Tuple, TYPE_CHECKING
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from ai.engine import AIEngine

# Classification levels (increasing sensitivity)
PUBLIC       = "public"
INTERNAL     = "internal"
CONFIDENTIAL = "confidential"
SECRET       = "secret"

LEVELS = [PUBLIC, INTERNAL, CONFIDENTIAL, SECRET]
LEVEL_ORDER = {PUBLIC: 0, INTERNAL: 1, CONFIDENTIAL: 2, SECRET: 3}

# Classification tag prefix
TAG_PREFIX = "class:"

# ── Pattern-based rules ─────────────────────────────────────────────────────

SECRET_PATTERNS = [
    # Private keys
    r"-----BEGIN\s+(?:RSA|EC|DSA|OPENSSH|PRIVATE)\s+(?:RSA\s+)?PRIVATE KEY",
    r"-----BEGIN\s+ENCRYPTED\s+PRIVATE\s+KEY",
    # API keys / tokens
    r"(?:api[_-]?key|apikey|api[_-]?secret|auth[_-]?token)\s*[=:]\s*['\"][A-Za-z0-9+/]{20,}",
    # AWS credentials
    r"AKIA[A-Z0-9]{16}",
    r"aws[_-]?secret[_-]?access[_-]?key\s*[=:]\s*['\"][^'\"]{20,}",
    # Passwords
    r"(?:password|passwd|pwd)\s*[=:]\s*['\"][^'\"]{6,}",
    # Database URLs with passwords
    r"(?:postgres|mysql|mongodb|redis)://[^:]+:[^@]+@",
    # JWT tokens
    r"eyJ[A-Za-z0-9+/]{50,}",
    # Private key content (base64 blocks)
    r"MII[A-Za-z0-9+/]{50,}={0,2}",
]

CONFIDENTIAL_PATTERNS = [
    # Credit cards
    r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13})\b",
    # Social security numbers
    r"\b\d{3}-\d{2}-\d{4}\b",
    # Email addresses in bulk
    r"(?:[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
    # Phone numbers
    r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
    # IP addresses (internal)
    r"\b(?:10\.|172\.(?:1[6-9]|2\d|3[01])\.|192\.168\.)\d+\.\d+\b",
    # Salary / financial
    r"\b(?:salary|compensation|ssn|tax[_-]?id|employer[_-]?id)\s*[=:]\s*[\$£€]?\d",
    # Health data keywords
    r"\b(?:diagnosis|prescription|medical[_-]?record|patient[_-]?id|dob)\b",
]

INTERNAL_PATTERNS = [
    # Internal paths
    r"/(?:internal|private|confidential|restricted)/",
    # Draft markers
    r"\b(?:DRAFT|INTERNAL|NOT FOR DISTRIBUTION|PROPRIETARY)\b",
    # TODO with sensitive context
    r"TODO.*(?:password|secret|key|token|credential)",
]

SECRET_KEYWORDS   = frozenset({"private_key","secret_key","password","credential","token"})
INTERNAL_KEYWORDS = frozenset({"internal","draft","confidential","restricted","proprietary"})


@dataclass
class ClassificationResult:
    """Result of classifying an object."""

    level:       str            # PUBLIC|INTERNAL|CONFIDENTIAL|SECRET
    confidence:  float          # 0.0–1.0
    reasons:     List[str]      # why this level was assigned
    ai_used:     bool = False   # was the AI engine consulted?


class DataClassifier:
    """
    Classifies SOS objects by sensitivity using regex + optional AI.
    """

    def __init__(self, ai: "AIEngine" = None):
        """Initialise with optional AI engine for ambiguous cases."""
        self.ai = ai
        self._compiled_secret       = [re.compile(p, re.I) for p in SECRET_PATTERNS]
        self._compiled_confidential = [re.compile(p, re.I) for p in CONFIDENTIAL_PATTERNS]
        self._compiled_internal     = [re.compile(p, re.I) for p in INTERNAL_PATTERNS]

    def classify(self, content: str, path: str = "",
                 existing_tags: List[str] = None) -> ClassificationResult:
        """
        Classify content by sensitivity level.

        Args:
            content (str): The text content to classify.
            path (str): The object path (provides naming context).
            existing_tags (List[str]): Existing tags on the object.

        Returns:
            ClassificationResult: The classification with confidence and reasons.
        """
        reasons: List[str] = []
        level = PUBLIC
        confidence = 0.9

        existing_tags = existing_tags or []
        # Check existing classification tags
        for tag in existing_tags:
            if tag.startswith(TAG_PREFIX):
                existing_level = tag[len(TAG_PREFIX):]
                if existing_level in LEVELS:
                    # Trust existing manual classification
                    return ClassificationResult(
                        level=existing_level, confidence=1.0,
                        reasons=["manually classified"], ai_used=False
                    )

        # Check path-based hints
        path_lower = path.lower()
        if any(k in path_lower for k in ("/secret", "/private", "/key", "/cred")):
            level = SECRET
            confidence = 0.95
            reasons.append(f"path contains sensitive keyword: {path}")
        elif any(k in path_lower for k in ("/confidential", "/sensitive", "/pii")):
            level = CONFIDENTIAL
            confidence = 0.9
            reasons.append(f"path suggests confidential content: {path}")
        elif any(k in path_lower for k in ("/internal", "/draft", "/restricted")):
            level = INTERNAL
            reasons.append("path suggests internal content")

        # Content-based classification
        text_sample = content[:8000]   # limit scan size

        # SECRET patterns (highest priority)
        for pattern in self._compiled_secret:
            if pattern.search(text_sample):
                if LEVEL_ORDER[SECRET] > LEVEL_ORDER[level]:
                    level = SECRET
                    confidence = 0.98
                reasons.append(f"secret pattern: {pattern.pattern[:40]}")
                break

        # CONFIDENTIAL patterns
        if LEVEL_ORDER[level] < LEVEL_ORDER[CONFIDENTIAL]:
            conf_hits = 0
            for pattern in self._compiled_confidential:
                if pattern.search(text_sample):
                    conf_hits += 1
                    reasons.append(f"confidential pattern match")
            if conf_hits >= 2:
                level = CONFIDENTIAL
                confidence = 0.85

        # INTERNAL patterns
        if LEVEL_ORDER[level] < LEVEL_ORDER[INTERNAL]:
            for pattern in self._compiled_internal:
                if pattern.search(text_sample):
                    level = INTERNAL
                    confidence = 0.75
                    reasons.append("internal marker found")
                    break

        # Keyword check on path
        fname = path.rsplit("/", 1)[-1].lower().replace(".", "_").replace("-", "_")
        fname_words = set(fname.split("_"))
        if fname_words & SECRET_KEYWORDS:
            if LEVEL_ORDER[SECRET] > LEVEL_ORDER[level]:
                level = SECRET
                confidence = 0.92
                reasons.append(f"filename contains sensitive keyword")

        # AI check for ambiguous cases (INTERNAL boundary)
        ai_used = False
        if (self.ai and level == PUBLIC and len(content) > 100
                and self.ai.tier != "rag"):
            ai_label = self._ai_classify(content[:500])
            if ai_label and ai_label in LEVELS:
                ai_used = True
                if LEVEL_ORDER[ai_label] > LEVEL_ORDER[level]:
                    level = ai_label
                    confidence = 0.7
                    reasons.append(f"AI classification: {ai_label}")

        if not reasons:
            reasons.append("no sensitive patterns detected")

        return ClassificationResult(
            level=level, confidence=confidence,
            reasons=reasons, ai_used=ai_used
        )

    def _ai_classify(self, sample: str) -> Optional[str]:
        """Ask the AI engine to classify a content sample."""
        prompt = (
            f"Classify this content as exactly one of: public, internal, confidential, secret.\n"
            f"Respond with only the single word.\n\nContent:\n{sample}"
        )
        try:
            result = self.ai.ask(prompt, max_tokens=10).strip().lower()
            for level in LEVELS:
                if level in result:
                    return level
        except Exception:
            pass
        return None


class ClassificationManager:
    """
    Manages data classification for the SOS.
    Auto-classifies objects on write and applies security controls.
    """

    def __init__(self, sos: "SemanticObjectStore",
                 ai: "AIEngine" = None):
        """Initialise the classification manager."""
        self.sos        = sos
        self.classifier = DataClassifier(ai)
        self._crypto    = None   # set by patch_with_crypto()
        self._caps      = None   # set by patch_with_capabilities()

    def set_crypto(self, crypto):
        """Attach a CryptoEngine for auto-encryption."""
        self._crypto = crypto

    def set_capabilities(self, caps):
        """Attach a CapabilityStore for access control on secrets."""
        self._caps = caps

    def classify_object(self, path: str) -> ClassificationResult:
        """
        Classify an existing SOS object.

        Args:
            path (str): The SOS path to classify.

        Returns:
            ClassificationResult: The classification result.
        """
        try:
            content = self.sos.read(path)
            tags    = self.sos.get_tags(path)
            result  = self.classifier.classify(content, path, tags)
            # Apply classification tag
            self._apply_classification(path, result.level)
            return result
        except Exception as e:
            return ClassificationResult(
                PUBLIC, 0.0, [f"error: {e}"])

    def _apply_classification(self, path: str, level: str):
        """Tag the object and apply security controls based on classification level."""
        # Remove old classification tags
        existing = self.sos.get_tags(path)
        for tag in existing:
            if tag.startswith(TAG_PREFIX):
                self.sos.untag(path, tag)
        # Add new tag
        self.sos.tag(path, TAG_PREFIX + level)
        # Apply controls
        if level == SECRET and self._crypto and not self._crypto.locked:
            # Ensure secret objects are also tagged for auto-encryption
            self.sos.tag(path, "secret")
        elif level == CONFIDENTIAL:
            self.sos.tag(path, "sensitive")

    def get_level(self, path: str) -> str:
        """Return the current classification level for a path."""
        for tag in self.sos.get_tags(path):
            if tag.startswith(TAG_PREFIX):
                return tag[len(TAG_PREFIX):]
        return PUBLIC

    def stats(self) -> Dict[str, int]:
        """Return object counts per classification level."""
        counts = {l: 0 for l in LEVELS}
        for level in LEVELS:
            tag   = TAG_PREFIX + level
            paths = self.sos.find_by_tag(tag, fuzzy=False)
            counts[level] = len(paths)
        return counts

    def patch_sos(self, user_fn=None):
        """
        Monkey-patch SOS to auto-classify objects on write.

        Args:
            user_fn: Callable returning the current username.
        """
        orig_write = self.sos.write
        manager    = self

        def _write(path, content, **kw):
            oid = orig_write(path, content, **kw)
            # Classify asynchronously to avoid write latency
            import threading
            def _classify():
                try:
                    manager.classify_object(path)
                except Exception:
                    pass
            threading.Thread(target=_classify, daemon=True).start()
            return oid

        self.sos.write = _write
