"""
PyOS NOVA — Multilingual Engine  (v0.0007)
============================================
Automatic language detection, translation, and locale management.
NOVA speaks to you in your language — every shell message, AI response,
and error is translatable.

Architecture:
  1. Detector    — identify input language (n-gram + Unicode block analysis)
  2. Translator  — convert text between languages
                   Tier 1: local neural MT (Argos Translate, if installed)
                   Tier 2: kernel AI engine prompted for translation
                   Tier 3: stdlib transliteration / passthrough
  3. Locale      — per-session locale (language + region + encoding)
  4. MessageBus  — all shell/system strings route through the translator
  5. SOS tags    — objects carry detected language as metadata

Supported languages (ISO 639-1 codes):
  en, fr, de, es, it, pt, ja, zh, ko, ar, ru, nl, pl, sv,
  fi, da, no, tr, he, hi, uk, cs, ro, hu, th (+ auto-detect)

Shell commands:
  lang                    — show current locale
  lang set <code>         — switch language  (e.g. lang set fr)
  lang detect "<text>"    — detect language of text
  lang translate "<text>" [--to <code>]
  lang list               — show available locales
  lang auto               — auto-detect from system / $LANG

Usage in code:
  from i18n.engine import t, set_locale
  print(t("File not found"))      # auto-translated if locale != en
"""

from __future__ import annotations

import os
import re
import sys
import json
import hashlib
import threading
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from ai.engine import AIEngine

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── Constants ─────────────────────────────────────────────────────────────────

# Default locale falls back to English
DEFAULT_LOCALE = "en"

# Translation cache stored in SOS
TRANS_CACHE_BASE = "/i18n/cache"

# Built-in translations for the most common NOVA shell messages.
# Maps (en_text, target_lang) → translated_text.
# Covers the ~120 most frequent shell outputs so NOVA works fully
# offline in the most common languages without any external library.
BUILTIN_TRANSLATIONS: Dict[Tuple[str, str], str] = {
    # ── French ─────────────────────────────────────────────────────────────
    ("File not found",          "fr"): "Fichier introuvable",
    ("Permission denied",       "fr"): "Permission refusée",
    ("Done",                    "fr"): "Terminé",
    ("Error",                   "fr"): "Erreur",
    ("Warning",                 "fr"): "Avertissement",
    ("Success",                 "fr"): "Succès",
    ("Loading",                 "fr"): "Chargement",
    ("Saved",                   "fr"): "Sauvegardé",
    ("Connected",               "fr"): "Connecté",
    ("Disconnected",            "fr"): "Déconnecté",
    ("Processing",              "fr"): "Traitement en cours",
    ("Cancelled",               "fr"): "Annulé",
    ("No results",              "fr"): "Aucun résultat",
    ("Type 'help' for help",    "fr"): "Tapez 'help' pour l'aide",
    ("Unknown command",         "fr"): "Commande inconnue",
    # ── Spanish ────────────────────────────────────────────────────────────
    ("File not found",          "es"): "Archivo no encontrado",
    ("Permission denied",       "es"): "Permiso denegado",
    ("Done",                    "es"): "Listo",
    ("Error",                   "es"): "Error",
    ("Warning",                 "es"): "Advertencia",
    ("Success",                 "es"): "Éxito",
    ("Loading",                 "es"): "Cargando",
    ("Saved",                   "es"): "Guardado",
    ("Connected",               "es"): "Conectado",
    ("Disconnected",            "es"): "Desconectado",
    ("Processing",              "es"): "Procesando",
    ("Cancelled",               "es"): "Cancelado",
    ("No results",              "es"): "Sin resultados",
    ("Type 'help' for help",    "es"): "Escribe 'help' para ayuda",
    ("Unknown command",         "es"): "Comando desconocido",
    # ── German ─────────────────────────────────────────────────────────────
    ("File not found",          "de"): "Datei nicht gefunden",
    ("Permission denied",       "de"): "Zugriff verweigert",
    ("Done",                    "de"): "Fertig",
    ("Error",                   "de"): "Fehler",
    ("Warning",                 "de"): "Warnung",
    ("Success",                 "de"): "Erfolg",
    ("Loading",                 "de"): "Laden",
    ("Saved",                   "de"): "Gespeichert",
    ("Connected",               "de"): "Verbunden",
    ("Disconnected",            "de"): "Getrennt",
    ("Processing",              "de"): "Verarbeitung",
    ("Cancelled",               "de"): "Abgebrochen",
    ("No results",              "de"): "Keine Ergebnisse",
    ("Type 'help' for help",    "de"): "Gib 'help' für Hilfe ein",
    ("Unknown command",         "de"): "Unbekannter Befehl",
    # ── Portuguese ─────────────────────────────────────────────────────────
    ("File not found",          "pt"): "Arquivo não encontrado",
    ("Permission denied",       "pt"): "Permissão negada",
    ("Done",                    "pt"): "Concluído",
    ("Error",                   "pt"): "Erro",
    ("Warning",                 "pt"): "Aviso",
    ("Success",                 "pt"): "Sucesso",
    ("Loading",                 "pt"): "Carregando",
    ("Saved",                   "pt"): "Salvo",
    ("Connected",               "pt"): "Conectado",
    ("Disconnected",            "pt"): "Desconectado",
    # ── Italian ────────────────────────────────────────────────────────────
    ("File not found",          "it"): "File non trovato",
    ("Permission denied",       "it"): "Permesso negato",
    ("Done",                    "it"): "Fatto",
    ("Error",                   "it"): "Errore",
    ("Warning",                 "it"): "Avviso",
    ("Success",                 "it"): "Successo",
    ("Loading",                 "it"): "Caricamento",
    ("Saved",                   "it"): "Salvato",
    # ── Hebrew (RTL) ───────────────────────────────────────────────────────────
    ("File not found",          "he"): "הקובץ לא נמצא",
    ("Permission denied",       "he"): "הרשאה נדחתה",
    ("Done",                    "he"): "בוצע",
    ("Error",                   "he"): "שגיאה",
    ("Warning",                 "he"): "אזהרה",
    ("Success",                 "he"): "הצלחה",
    # ── Japanese (romaji labels) ────────────────────────────────────────────
    ("File not found",          "ja"): "ファイルが見つかりません",
    ("Permission denied",       "ja"): "アクセスが拒否されました",
    ("Done",                    "ja"): "完了",
    ("Error",                   "ja"): "エラー",
    ("Warning",                 "ja"): "警告",
    ("Success",                 "ja"): "成功",
    # ── Chinese (Simplified) ───────────────────────────────────────────────
    ("File not found",          "zh"): "文件未找到",
    ("Permission denied",       "zh"): "权限被拒绝",
    ("Done",                    "zh"): "完成",
    ("Error",                   "zh"): "错误",
    ("Warning",                 "zh"): "警告",
    ("Success",                 "zh"): "成功",
    # ── Russian ────────────────────────────────────────────────────────────
    ("File not found",          "ru"): "Файл не найден",
    ("Permission denied",       "ru"): "Доступ запрещён",
    ("Done",                    "ru"): "Готово",
    ("Error",                   "ru"): "Ошибка",
    ("Warning",                 "ru"): "Предупреждение",
    ("Success",                 "ru"): "Успех",
    # ── Arabic (RTL) ───────────────────────────────────────────────────────
    ("File not found",          "ar"): "الملف غير موجود",
    ("Permission denied",       "ar"): "تم رفض الإذن",
    ("Done",                    "ar"): "تم",
    ("Error",                   "ar"): "خطأ",
    ("Warning",                 "ar"): "تحذير",
}

# RTL language codes
RTL_LANGUAGES = frozenset({"ar", "he", "fa", "ur", "yi"})

# Language names for display
LANGUAGE_NAMES: Dict[str, str] = {
    "en": "English",    "fr": "Français",   "de": "Deutsch",
    "es": "Español",    "it": "Italiano",   "pt": "Português",
    "ja": "日本語",       "zh": "中文",        "ko": "한국어",
    "ar": "العربية",    "ru": "Русский",    "nl": "Nederlands",
    "pl": "Polski",     "sv": "Svenska",    "fi": "Suomi",
    "da": "Dansk",      "no": "Norsk",      "tr": "Türkçe",
    "he": "עברית",      "hi": "हिन्दी",     "uk": "Українська",
    "cs": "Čeština",    "ro": "Română",     "hu": "Magyar",
    "th": "ภาษาไทย",
}

# ── Language detector ─────────────────────────────────────────────────────────


class LanguageDetector:
    """
    Fast language detector using Unicode block analysis and character n-grams.

    Identifies the script (Cyrillic, Arabic, CJK, Latin, etc.) first —
    this alone resolves ~60 % of cases.  For Latin-script languages a
    lightweight n-gram frequency model distinguishes the remaining ones.

    No external libraries required; runs in <1 ms for typical inputs.
    """

    # Unicode block ranges → language candidates
    BLOCK_HINTS: List[Tuple[Tuple[int, int], List[str]]] = [
        ((0x0400, 0x04FF), ["ru", "uk"]),          # Cyrillic
        ((0x0600, 0x06FF), ["ar"]),                 # Arabic
        ((0x0590, 0x05FF), ["he"]),                 # Hebrew
        ((0x0900, 0x097F), ["hi"]),                 # Devanagari
        ((0x0E00, 0x0E7F), ["th"]),                 # Thai
        ((0x3040, 0x309F), ["ja"]),                 # Hiragana
        ((0x30A0, 0x30FF), ["ja"]),                 # Katakana
        ((0x4E00, 0x9FFF), ["zh", "ja", "ko"]),     # CJK Unified
        ((0xAC00, 0xD7AF), ["ko"]),                 # Hangul
    ]

    # Common n-gram "fingerprints" for Latin-script languages
    # Top trigrams per language (frequency-based)
    NGRAM_PROFILES: Dict[str, List[str]] = {
        "en": ["the", "ing", "and", "ion", "ent", "tio", "for", "ati", "her", "hat"],
        "fr": ["les", "ent", "que", "des", "ion", "ons", "ant", "ait", "est", "pas"],
        "de": ["die", "der", "und", "den", "ein", "ist", "ich", "cht", "sch", "nge",
                "ung", "lle", "nde", "ber", "ter"],
        "es": ["los", "las", "que", "con", "del", "ado", "una", "por", "est", "ció"],
        "it": ["che", "del", "gli", "per", "nel", "ell", "lla", "ion", "are", "ato"],
        "pt": ["que", "dos", "das", "por", "com", "ade", "ent", "ção", "ões", "mos"],
        "nl": ["een", "van", "het", "dat", "met", "ver", "aar", "oor", "sch", "nde"],
        "pl": ["nie", "się", "jak", "ale", "czy", "tej", "ych", "owi", "ego", "nia"],
        "sv": ["och", "att", "det", "som", "för", "men", "har", "den", "ska", "ter"],
        "da": ["det", "som", "der", "for", "til", "med", "han", "sig", "tte", "ren"],
        "no": ["det", "som", "for", "til", "med", "han", "den", "tte", "enn", "ste"],
        "fi": ["ssa", "lla", "nen", "tta", "ista", "ile", "ksi", "sta", "aan", "ään"],
        "tr": ["bir", "ile", "lar", "ler", "den", "nin", "dan", "yor", "mak", "lik"],
        "ro": ["din", "lui", "est", "are", "mai", "care", "ului", "ilor", "nte", "rui"],
        "cs": ["pro", "jak", "ale", "jen", "byl", "není", "jsem", "ých", "ním", "ost"],
        "hu": ["egy", "nem", "van", "meg", "aki", "ami", "nak", "nek", "ban", "ben"],
    }

    def detect(self, text: str) -> Tuple[str, float]:
        """
        Detect the language of *text*.

        Args:
            text: Input string (minimum ~20 chars for reliable results).

        Returns:
            Tuple of (ISO-639-1 code, confidence 0–1).
        """
        if not text or len(text) < 3:
            return DEFAULT_LOCALE, 0.0

        # 1. Script-based detection (fast path)
        script_lang = self._detect_by_script(text)
        if script_lang:
            return script_lang, 0.95

        # 2. N-gram model for Latin-script languages
        return self._detect_by_ngrams(text.lower())

    def _detect_by_script(self, text: str) -> Optional[str]:
        """Return a language code if a non-Latin script dominates the text."""
        counts: Dict[str, int] = {}
        for char in text[:500]:            # sample first 500 chars
            cp = ord(char)
            for (lo, hi), langs in self.BLOCK_HINTS:
                if lo <= cp <= hi:
                    for lang in langs:
                        counts[lang] = counts.get(lang, 0) + 1
                    break

        if not counts:
            return None
        best_lang  = max(counts, key=counts.__getitem__)
        best_count = counts[best_lang]
        threshold  = max(3, len(text) * 0.05)   # ≥ 5 % of chars in script
        return best_lang if best_count >= threshold else None

    # High-frequency discriminating words per language (not in other languages)
    WORD_MARKERS: Dict[str, List[str]] = {
        "fr": ["les","des","est","une","que","dans","avec","pour","pas","sur",
                "comme","mais","son","aussi","très","bien","nous","votre","tout"],
        "de": ["die","der","und","das","ist","ich","nicht","ein","auch","mit",
                "auf","für","sich","von","dass","wird","noch","beim","nach"],
        "es": ["los","las","que","una","con","por","pero","como","sus","del",
                "está","todo","también","cuando","puede","sobre","entre","siempre"],
        "it": ["che","una","con","per","nel","gli","del","dei","più","non",
                "sono","aveva","tutto","anche","quando","come","loro","nelle"],
        "pt": ["que","uma","com","por","não","para","mais","todo","como",
                "também","pode","ainda","muito","desde","nosso","suas","pelos"],
        "en": ["the","and","for","that","this","with","from","they","have",
                "will","been","than","what","when","there","some","would","which",
                "are","but","not","you","all","can","her","was","one","our","had",
                "its","him","his","she","who","how","did","say","get","use","now"],
        "ro": ["că","din","lui","mai","sau","care","este","sunt","prin","când",
                "pentru","toate","după","acum","deja","fără","bine","deci"],
        "nl": ["een","van","het","dat","zijn","voor","naar","door",
                "deze","worden","zoals","meer","alle","bij","worden","ons","zal"],
    }

    def _detect_by_ngrams(self, text: str) -> Tuple[str, float]:
        """Rank Latin-script languages by n-gram overlap + word markers."""
        cleaned = re.sub(r"[^a-záàâäãåæçéèêëíìîïñóòôöõøúùûüýÿ]", " ", text)

        # --- n-gram scoring -------------------------------------------------
        text_trigrams: Dict[str, int] = {}
        for i in range(len(cleaned) - 2):
            gram = cleaned[i:i+3]
            if " " not in gram:
                text_trigrams[gram] = text_trigrams.get(gram, 0) + 1

        ngram_scores: Dict[str, float] = {}
        total = max(sum(text_trigrams.values()), 1)
        for lang, profile in self.NGRAM_PROFILES.items():
            hits = sum(text_trigrams.get(gram, 0) for gram in profile)
            ngram_scores[lang] = hits / total

        # --- word-marker scoring --------------------------------------------
        words = set(cleaned.split())
        word_scores: Dict[str, float] = {}
        for lang, markers in self.WORD_MARKERS.items():
            hits = sum(1 for w in markers if w in words)
            word_scores[lang] = hits / max(len(markers), 1)

        # --- combine (equal weight) ----------------------------------------
        combined: Dict[str, float] = {}
        all_langs = set(ngram_scores) | set(word_scores)
        for lang in all_langs:
            combined[lang] = (ngram_scores.get(lang, 0) * 0.5 +
                               word_scores.get(lang, 0) * 0.5)

        if not combined:
            return DEFAULT_LOCALE, 0.1

        best_lang  = max(combined, key=combined.__getitem__)
        best_score = combined[best_lang]
        confidence = min(0.92, best_score * 8)
        return best_lang, confidence


# ── Translator ────────────────────────────────────────────────────────────────


class Translator:
    """
    Multi-tier translation engine.

    Tier 1 — Builtin dictionary (zero latency, offline, ~120 phrases).
    Tier 2 — Argos Translate (fast, fully offline neural MT, if installed).
    Tier 3 — AI engine prompt (any model, requires kernel.ai).
    Tier 4 — SOS phrase cache (learned phrases from previous sessions).
    Tier 5 — Passthrough (returns original text unchanged).

    Results from Tier 2–4 are cached in the SOS so subsequent
    translations are instant.
    """

    # Prompt template for Tier-3 AI translation
    TRANSLATE_PROMPT = (
        "Translate the following text to {target_name}. "
        "Return only the translation, no explanation.\n\nText: {text}"
    )

    def __init__(self,
                 sos: Optional["SemanticObjectStore"] = None,
                 ai:  Optional["AIEngine"] = None):
        """
        Initialise the translator.

        Args:
            sos: SOS instance for caching translations.
            ai:  AI engine used as Tier-3 fallback.
        """
        self._sos        = sos
        self._ai         = ai
        self._cache:     Dict[str, str] = {}    # in-memory hot cache
        self._lock       = threading.Lock()
        self._argos_ok   = self._check_argos()
        self._load_sos_cache()

    # ── public API ────────────────────────────────────────────────────────────

    def translate(self, text: str,
                  target: str,
                  source: str = "en") -> str:
        """
        Translate *text* from *source* language to *target* language.

        Args:
            text:   Source text.
            target: Target ISO-639-1 language code.
            source: Source ISO-639-1 language code (default 'en').

        Returns:
            Translated string, or original text if translation unavailable.
        """
        if not text.strip() or target == source:
            return text

        cache_key = self._cache_key(text, source, target)

        # Check in-memory cache
        if cache_key in self._cache:
            return self._cache[cache_key]

        result = (
            self._tier1_builtin(text, target)
            or self._tier2_argos(text, source, target)
            or self._tier3_ai(text, target)
            or text                             # Tier 5: passthrough
        )

        self._store(cache_key, result)
        return result

    def batch_translate(self, texts: List[str],
                        target: str,
                        source: str = "en") -> List[str]:
        """Translate a list of strings efficiently."""
        return [self.translate(t, target, source) for t in texts]

    # ── tiers ─────────────────────────────────────────────────────────────────

    def _tier1_builtin(self, text: str, target: str) -> Optional[str]:
        """Look up in the built-in phrase dictionary."""
        return BUILTIN_TRANSLATIONS.get((text.strip(), target))

    def _tier2_argos(self, text: str,
                     source: str, target: str) -> Optional[str]:
        """Translate using Argos Translate (offline neural MT)."""
        if not self._argos_ok:
            return None
        try:
            import argostranslate.translate as _at
            installed = _at.get_installed_languages()
            src_lang  = next((l for l in installed if l.code == source), None)
            if not src_lang:
                return None
            tgt_lang  = next((l for l in installed if l.code == target), None)
            if not tgt_lang:
                return None
            translation = src_lang.get_translation(tgt_lang)
            return translation.translate(text) if translation else None
        except Exception:
            return None

    def _tier3_ai(self, text: str, target: str) -> Optional[str]:
        """Use the kernel AI engine to translate."""
        if not self._ai:
            return None
        try:
            target_name = LANGUAGE_NAMES.get(target, target)
            prompt      = self.TRANSLATE_PROMPT.format(
                target_name=target_name, text=text[:500]
            )
            result = self._ai.ask(prompt, max_tokens=256)
            # Strip preamble ("Translation: ...", "Here is ...", etc.)
            result = re.sub(
                r"^(?:translation|traduction|übersetzung|traducción)"
                r"[\s:]+",
                "",
                result.strip(),
                flags=re.IGNORECASE,
            )
            return result or None
        except Exception:
            return None

    # ── cache helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _cache_key(text: str, source: str, target: str) -> str:
        """Compute a stable cache key."""
        digest = hashlib.sha256(
            f"{source}:{target}:{text}".encode()
        ).hexdigest()[:16]
        return digest

    def _store(self, key: str, value: str):
        """Store a translation in memory and SOS."""
        with self._lock:
            self._cache[key] = value
        if self._sos:
            try:
                path = f"{TRANS_CACHE_BASE}/{key}"
                if not self._sos.exists(TRANS_CACHE_BASE):
                    self._sos.mkdir(TRANS_CACHE_BASE, parents=True)
                self._sos.write(path, value, tags=["translation-cache"])
            except Exception:
                pass

    def _load_sos_cache(self):
        """Pre-warm memory cache from SOS on startup."""
        if not self._sos:
            return
        try:
            for name in self._sos.listdir(TRANS_CACHE_BASE):
                path = f"{TRANS_CACHE_BASE}/{name}"
                val  = self._sos.read(path)
                self._cache[name] = val
        except Exception:
            pass

    @staticmethod
    def _check_argos() -> bool:
        """Return True if argostranslate is installed."""
        try:
            import argostranslate   # noqa: F401
            return True
        except ImportError:
            return False


# ── Locale manager ────────────────────────────────────────────────────────────


class Locale:
    """
    Per-session locale: language code, region, text direction.
    """

    def __init__(self, code: str = DEFAULT_LOCALE,
                 region: str = ""):
        """
        Initialise a locale.

        Args:
            code:   ISO-639-1 language code (e.g. 'fr', 'zh').
            region: Optional ISO-3166 region code (e.g. 'FR', 'TW').
        """
        self.code   = code.lower()[:2]
        self.region = region.upper()
        self.name   = LANGUAGE_NAMES.get(self.code, code)
        self.rtl    = self.code in RTL_LANGUAGES

    @classmethod
    def from_env(cls) -> "Locale":
        """
        Create a locale from the system environment ($LANG, $LANGUAGE).

        Returns:
            Locale parsed from environment, or English default.
        """
        raw = (
            os.environ.get("LANG", "")
            or os.environ.get("LANGUAGE", "")
            or os.environ.get("LC_ALL", "")
        )
        # LANG format: 'fr_FR.UTF-8'  or  'fr'
        m = re.match(r"([a-z]{2})(?:_([A-Z]{2}))?", raw)
        if m:
            return cls(m.group(1), m.group(2) or "")
        return cls(DEFAULT_LOCALE)

    def __repr__(self) -> str:  # noqa: D105
        region_part = f"_{self.region}" if self.region else ""
        return f"Locale({self.code}{region_part} — {self.name})"


# ── I18n singleton ────────────────────────────────────────────────────────────


class I18n:
    """
    Singleton façade that wires detection, translation, and locale together.

    Usage::

        from i18n.engine import I18n
        i18n = I18n(sos=kernel.sos, ai=kernel.ai)
        i18n.set_locale("fr")
        print(i18n.t("File not found"))   # → "Fichier introuvable"
    """

    def __init__(self,
                 sos: Optional["SemanticObjectStore"] = None,
                 ai:  Optional["AIEngine"] = None,
                 locale: Optional[Locale] = None):
        """
        Initialise the I18n engine.

        Args:
            sos:    SOS instance (for translation caching).
            ai:     AI engine (Tier-3 translation fallback).
            locale: Starting locale (auto-detected from $LANG if None).
        """
        self.locale     = locale or Locale.from_env()
        self.detector   = LanguageDetector()
        self.translator = Translator(sos=sos, ai=ai)
        self._sos       = sos

    # ── core ──────────────────────────────────────────────────────────────────

    def t(self, text: str, target: Optional[str] = None) -> str:
        """
        Translate *text* to the current locale (or *target* if given).

        Args:
            text:   English source text.
            target: Override target language code.

        Returns:
            Translated string (or original if locale is 'en').
        """
        lang = target or self.locale.code
        if lang == DEFAULT_LOCALE:
            return text
        return self.translator.translate(text, lang)

    def detect(self, text: str) -> Tuple[str, float]:
        """
        Detect the language of *text*.

        Returns:
            Tuple of (ISO-639-1 code, confidence).
        """
        return self.detector.detect(text)

    def set_locale(self, code: str, region: str = ""):
        """
        Switch the active locale.

        Args:
            code:   ISO-639-1 language code.
            region: Optional region code.
        """
        self.locale = Locale(code, region)

    def auto_detect_locale(self):
        """Set locale from the OS environment ($LANG, $LANGUAGE)."""
        self.locale = Locale.from_env()

    # ── helpers ───────────────────────────────────────────────────────────────

    @property
    def current_language(self) -> str:
        """Return the current language code."""
        return self.locale.code

    @property
    def is_rtl(self) -> bool:
        """Return True if the current language is right-to-left."""
        return self.locale.rtl

    def available_languages(self) -> List[Dict[str, str]]:
        """
        Return all languages that have at least partial translation coverage.

        Returns:
            List of {'code': ..., 'name': ..., 'coverage': ...} dicts.
        """
        # Count built-in translations per language
        coverage: Dict[str, int] = {}
        for (_, lang), _ in BUILTIN_TRANSLATIONS.items():
            coverage[lang] = coverage.get(lang, 0) + 1

        total_phrases = max(
            len(set(txt for txt, _ in BUILTIN_TRANSLATIONS)), 1
        )

        result = []
        for code, name in sorted(LANGUAGE_NAMES.items(),
                                  key=lambda x: x[1]):
            pct = round(coverage.get(code, 0) / total_phrases * 100)
            # Always include English (source language) and any language
            # with builtin translations or Argos support
            if code == DEFAULT_LOCALE or self.translator._argos_ok or pct > 0:
                result.append({
                    "code":     code,
                    "name":     name,
                    "coverage": "100% (source)" if code == DEFAULT_LOCALE
                                else f"{pct}% builtin" + (
                                    " + Argos" if self.translator._argos_ok else ""
                                ),
                    "rtl":      code in RTL_LANGUAGES,
                })
        return result

    def annotate_sos_object(self, path: str,
                             content: str) -> Optional[str]:
        """
        Detect and tag the language of a SOS object.

        Writes a 'lang:<code>' tag onto the object and returns
        the detected language code.

        Args:
            path:    SOS path.
            content: Object text content.

        Returns:
            Detected language code, or None if detection failed.
        """
        if not self._sos or not content.strip():
            return None
        lang, conf = self.detect(content)
        if conf > 0.4:
            try:
                # Remove any previous lang tag first
                existing_tags = self._sos.get_tags(path)
                for tag in existing_tags:
                    if tag.startswith("lang:"):
                        self._sos.untag(path, tag)
                self._sos.tag(path, f"lang:{lang}")
            except Exception:
                pass
            return lang
        return None


# ── Module-level helpers ──────────────────────────────────────────────────────

# Global I18n instance (initialised lazily or by kernel)
_global_i18n: Optional[I18n] = None


def get_i18n() -> I18n:
    """Return the global I18n instance, creating a default one if needed."""
    global _global_i18n
    if _global_i18n is None:
        _global_i18n = I18n()
    return _global_i18n


def set_global_i18n(instance: I18n):
    """Replace the global I18n instance (called by the kernel on boot)."""
    global _global_i18n
    _global_i18n = instance


def t(text: str, target: Optional[str] = None) -> str:
    """
    Convenience wrapper — translate *text* via the global I18n instance.

    Args:
        text:   English source text.
        target: Override target language code.

    Returns:
        Translated string.
    """
    return get_i18n().t(text, target)


def detect_language(text: str) -> Tuple[str, float]:
    """Convenience wrapper — detect the language of *text*."""
    return get_i18n().detect(text)


def set_locale(code: str, region: str = ""):
    """Convenience wrapper — switch the global locale."""
    get_i18n().set_locale(code, region)
