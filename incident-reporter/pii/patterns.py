"""Compiled PII detection patterns.

Each ``Pattern`` is a named tuple of (name, regex, replacement).
The scrubber applies them in declaration order; credit-card comes first because
its digit sequences are longest and most specific, reducing false positives from
later patterns treating card fragments as phone-number groups.
"""

import re
from typing import NamedTuple


class Pattern(NamedTuple):
    name: str
    regex: re.Pattern
    replacement: str


PATTERNS: list[Pattern] = [
    # ── Credit card numbers ────────────────────────────────────────────────
    # Contiguous (no separator) forms: Visa 13/16, MC 16, Amex 15, Discover 16
    # Grouped forms: 4 groups of 4 separated by space or dash (Visa/MC/Discover),
    #                or the Amex 4-6-5 grouping.
    Pattern(
        name="credit_card",
        regex=re.compile(
            r'\b(?:'
            r'4[0-9]{12}(?:[0-9]{3})?'                         # Visa 13 or 16
            r'|5[1-5][0-9]{14}'                                 # Mastercard 16
            r'|3[47][0-9]{13}'                                  # Amex 15
            r'|6(?:011|5[0-9]{2})[0-9]{12}'                    # Discover 16
            r')\b'
            r'|'
            # Separated groups: leading card prefix + 3 more 4-digit groups
            r'\b(?:4[0-9]{3}|5[1-5][0-9]{2}|3[47][0-9]|6(?:011|5[0-9]{2}))'
            r'(?:[-\s][0-9]{4}){3}\b'
            r'|'
            # Amex grouped: 4-6-5
            r'\b3[47][0-9]{2}[-\s][0-9]{6}[-\s][0-9]{5}\b'
        ),
        replacement="[CC REDACTED]",
    ),

    # ── Email addresses ────────────────────────────────────────────────────
    Pattern(
        name="email",
        regex=re.compile(
            r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'
        ),
        replacement="[EMAIL REDACTED]",
    ),

    # ── Phone numbers (US / international) ───────────────────────────────
    # Matches: (555) 123-4567 | 555-123-4567 | 555.123.4567 | +1 555 123 4567
    # Uses (?<!\w) instead of \b so patterns starting with "(" are caught —
    # \b does not fire before a non-word character like an opening parenthesis.
    Pattern(
        name="phone",
        regex=re.compile(
            r'(?<!\w)(?:'
            r'(?:\+?1[-.\s]?)?'                             # optional country code
            r'(?:\(\d{3}\)|\d{3})'                          # area code
            r'[-.\s]\d{3}[-.\s]\d{4}'                       # subscriber number
            r'|\+1[-.\s]\d{3}[-.\s]\d{3}[-.\s]\d{4}'       # +1 with all separators
            r')(?!\d)'
        ),
        replacement="[PHONE REDACTED]",
    ),

    # ── IPv4 addresses ────────────────────────────────────────────────────
    # Strict octet validation (0–255) to avoid version-number false positives.
    Pattern(
        name="ipv4",
        regex=re.compile(
            r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}'
            r'(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b'
        ),
        replacement="[IPv4 REDACTED]",
    ),

    # ── IPv6 addresses ────────────────────────────────────────────────────
    # Covers: full 8-group, compressed (::), and mixed compressed forms.
    Pattern(
        name="ipv6",
        regex=re.compile(
            r'(?:'
            r'(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}'         # full 8-group
            r'|(?:[0-9a-fA-F]{1,4}:){1,7}:'                       # trailing ::
            r'|:(?::[0-9a-fA-F]{1,4}){1,7}'                       # leading ::
            r'|(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}'      # one :: in middle
            r'|(?:[0-9a-fA-F]{1,4}:){1,5}(?::[0-9a-fA-F]{1,4}){1,2}'
            r'|(?:[0-9a-fA-F]{1,4}:){1,4}(?::[0-9a-fA-F]{1,4}){1,3}'
            r'|(?:[0-9a-fA-F]{1,4}:){1,3}(?::[0-9a-fA-F]{1,4}){1,4}'
            r'|(?:[0-9a-fA-F]{1,4}:){1,2}(?::[0-9a-fA-F]{1,4}){1,5}'
            r'|[0-9a-fA-F]{1,4}:(?::[0-9a-fA-F]{1,4}){1,6}'
            r')'
        ),
        replacement="[IPv6 REDACTED]",
    ),
]
