"""General-purpose utility functions for the OLamo application.

This module houses utility functions that are not specific to pipeline
orchestration (those live in helpers.py).
"""

__all__ = ["reverse_string"]


def reverse_string(s: str) -> str:
    """Return a reversed copy of *s*.

    Args:
        s: Input string (or any sequence supporting slicing).

    Returns:
        A new string with characters in reverse order.

    Examples:
        >>> reverse_string("hello")
        'olleh'
        >>> reverse_string("")
        ''
        >>> reverse_string("OLamo")
        'omaLO'
        >>> reverse_string("racecar")
        'racecar'
        >>> reverse_string("café")
        'éfac'

    Note:
        The type hint ``str`` is advisory. Any sliceable sequence (list,
        tuple, etc.) will work, but return type may differ. For non-string
        inputs, results follow standard Python slicing semantics.
    """
    return s[::-1]
