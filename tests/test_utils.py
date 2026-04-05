"""Tests for app.pipeline.utils."""

from app.pipeline.utils import reverse_string


class TestReverseString:
    def test_standard_ascii(self):
        assert reverse_string("hello") == "olleh"

    def test_empty_string(self):
        assert reverse_string("") == ""

    def test_single_character(self):
        assert reverse_string("x") == "x"

    def test_unicode(self):
        assert reverse_string("café") == "éfac"

    def test_palindrome(self):
        assert reverse_string("racecar") == "racecar"

    def test_mixed_case(self):
        assert reverse_string("OLamo") == "omaLO"

    def test_idempotency(self):
        """Reversing twice returns the original string."""
        s = "hello world"
        assert reverse_string(reverse_string(s)) == s

    def test_pure_function_no_side_effects(self):
        """Calling reverse_string does not modify the original string."""
        original = "OLamo"
        result = reverse_string(original)
        assert result == "omaLO"
        assert original == "OLamo"  # unchanged

    def test_import_from_app_pipeline_utils(self):
        """Direct import from the defining module works."""
        from app.pipeline.utils import reverse_string as rs_direct
        assert rs_direct("hello") == "olleh"

    def test_import_from_app_pipeline(self):
        """Import from the pipeline package works."""
        from app.pipeline import reverse_string as rs_pipeline
        assert rs_pipeline("hello") == "olleh"

    def test_import_from_app(self):
        """Import from the top-level app package works."""
        from app import reverse_string as rs_app
        assert rs_app("hello") == "olleh"
