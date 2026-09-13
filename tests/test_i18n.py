"""
PyOS NOVA — i18n Tests  (v0.0008)
===================================
Tests for language detection, translation, locale management, and
the I18n façade.
"""
from __future__ import annotations
import pytest


class TestLanguageDetector:
    @pytest.fixture
    def det(self):
        from i18n.engine import LanguageDetector
        return LanguageDetector()

    @pytest.mark.parametrize("text,expected", [
        ("The quick brown fox jumps over the lazy dog. This is English text here.", "en"),
        ("Les renards bruns sautent par-dessus les chiens paresseux dans les jardins", "fr"),
        ("Die schnelle Fuchs springt über den faulen Hund in Deutschland täglich", "de"),
        ("Los zorros también saltan sobre los perros en España muchas veces al día", "es"),
        ("これは日本語のテキストです。自動検出テスト。", "ja"),
        ("هذا نص عربي طويل مكتوب باللغة العربية للاختبار", "ar"),
        ("这是中文文本测试的详细内容和分析", "zh"),
    ])
    def test_detect(self, det, text, expected):
        code, conf = det.detect(text)
        assert code == expected, f"Expected {expected!r}, got {code!r} (conf={conf:.0%})"
        assert 0.0 <= conf <= 1.0

    def test_short_text_returns_default(self, det):
        from i18n.engine import DEFAULT_LOCALE
        code, conf = det.detect("")
        assert code == DEFAULT_LOCALE

    def test_confidence_range(self, det):
        for text in ["Hello world", "Bonjour monde", "Hallo Welt"]:
            _, conf = det.detect(text)
            assert 0.0 <= conf <= 1.0


class TestTranslator:
    @pytest.fixture
    def tr(self, sos):
        from i18n.engine import Translator
        return Translator(sos=sos)

    @pytest.mark.parametrize("text,lang,expected", [
        ("File not found", "fr", "Fichier introuvable"),
        ("Error",          "de", "Fehler"),
        ("Done",           "es", "Listo"),
        ("Done",           "ja", "完了"),
        ("Error",          "he", "שגיאה"),
        ("Success",        "ru", "Успех"),
        ("Warning",        "it", "Avviso"),
    ])
    def test_builtin_translation(self, tr, text, lang, expected):
        assert tr.translate(text, lang) == expected

    def test_same_language_passthrough(self, tr):
        assert tr.translate("Hello world", "en") == "Hello world"

    def test_unknown_text_passthrough(self, tr):
        result = tr.translate("xyzzy_untranslatable_phrase_42", "fr")
        assert result == "xyzzy_untranslatable_phrase_42"

    def test_cache_hit(self, tr):
        tr.translate("Done", "fr")
        result = tr.translate("Done", "fr")
        assert result == "Terminé"


class TestLocale:
    def test_from_code(self):
        from i18n.engine import Locale
        loc = Locale("fr", "FR")
        assert loc.code == "fr"
        assert loc.name == "Français"
        assert not loc.rtl

    def test_rtl_arabic(self):
        from i18n.engine import Locale
        assert Locale("ar").rtl

    def test_rtl_hebrew(self):
        from i18n.engine import Locale
        assert Locale("he").rtl

    def test_non_rtl(self):
        from i18n.engine import Locale
        for code in ("en", "fr", "de", "zh", "ja"):
            assert not Locale(code).rtl

    def test_from_env_default(self, monkeypatch):
        from i18n.engine import Locale, DEFAULT_LOCALE
        monkeypatch.setenv("LANG", "")
        monkeypatch.setenv("LANGUAGE", "")
        loc = Locale.from_env()
        assert loc.code == DEFAULT_LOCALE


class TestI18nFacade:
    @pytest.fixture
    def i18n(self, sos):
        from i18n.engine import I18n
        return I18n(sos=sos)

    def test_translate_fr(self, i18n):
        i18n.set_locale("fr")
        assert i18n.t("Done") == "Terminé"

    def test_translate_en_passthrough(self, i18n):
        i18n.set_locale("en")
        assert i18n.t("Done") == "Done"

    def test_available_languages_includes_en(self, i18n):
        codes = {l["code"] for l in i18n.available_languages()}
        assert "en" in codes

    def test_rtl_in_available_languages(self, i18n):
        rtl = {l["code"] for l in i18n.available_languages() if l["rtl"]}
        assert "ar" in rtl

    def test_global_helpers(self, sos):
        from i18n.engine import set_global_i18n, I18n, t, set_locale
        inst = I18n(sos=sos)
        set_global_i18n(inst)
        inst.set_locale("de")
        assert t("Error") == "Fehler"
        set_locale("en")
        assert t("Error") == "Error"
