"""A basic plausibility check on rule-based parser output, run right
after a rule's own parse() returns records and before those records are
ever trusted (extraction.rules.try_rules).

Confirmed real (2026-07): MetSpace sent a second, structurally different
email template ("Office Of The Week", a single-listing spotlight) that
extraction.rules.metspace was never built for. Its own area-header
anchor logic found no area line to trim the buffered text on, so the
ENTIRE email signature, "Sent:"/"To:"/"Subject:" header lines, and legal
disclaimer text ahead of the one real "Sqft:" line ended up verbatim in
Building (and, via schema.normalize_record, Property Address 1) —
thousands of characters of boilerplate in a field meant to hold a
building name. The rule matched (detect() correctly recognized this as
a MetSpace email) and parse() ran without raising, so nothing else in
the pipeline had any reason to distrust the result.

Deliberately generic — this has no MetSpace-specific knowledge at all,
so it applies uniformly to every rule-based parser (Knotel, MetSpace,
GPE, Kitts/Grid, BC, Breezblok) and will catch the SAME class of
failure the next time any provider introduces a template variant a rule
wasn't built for, not just this one already-seen case.
"""
import re

# Fields expected to hold a short, single piece of data, never a
# multi-sentence block of prose — a rule's own parse() putting a
# genuinely oversized value into one of these is itself implausible,
# regardless of what that value actually says. Deliberately generous
# (200 chars is several times longer than the longest genuine real
# value seen across every rule's own fixtures) so this never trips on
# real data, only on the kind of wholesale text dump confirmed above.
_SHORT_FIELDS = ("Building", "Area", "Floor/Unit")
_MAX_SHORT_FIELD_LENGTH = 200

# Checked more leniently (no length cap — a real Contacts string
# legitimately grows with every additional agent) but still rejected if
# it contains obvious email header/signature/disclaimer boilerplate.
_LENIENT_FIELDS = ("Contacts",)

# Substrings that are essentially certain to be raw email header/
# signature/legal-disclaimer text that leaked through, not real listing
# data — the first five are confirmed real substrings from MetSpace's
# own "Office Of The Week" garbage output; "Sent:"/"Subject:"/"From:"/
# "To:" catch a quoted email's own header block more generally.
_BOILERPLATE_RE = re.compile(
    r"IMPORTANT:\s*This e-?mail is intended"
    r"|privileged and confidential"
    r"|without prejudice"
    r"|does not accept liability"
    r"|copy,\s*distribute or take action"
    r"|\bSent:\s"
    r"|\bSubject:\s"
    r"|\bFrom:\s"
    r"|\bTo:\s",
    re.IGNORECASE,
)


def records_look_plausible(records):
    """True if every record looks like real, structurally sane listing
    data — False if ANY record has a field that's suspiciously long for
    what it's supposed to hold, or contains obvious email header/
    signature/disclaimer boilerplate. The caller (extraction.rules.
    try_rules) should treat False the same as the rule not matching at
    all and fall back to the LLM, rather than silently accepting
    garbage just because parse() ran without raising."""
    for record in records:
        for field in _SHORT_FIELDS:
            value = record.get(field)
            if isinstance(value, str) and len(value) > _MAX_SHORT_FIELD_LENGTH:
                return False
        for field in _SHORT_FIELDS + _LENIENT_FIELDS:
            value = record.get(field)
            if isinstance(value, str) and value and _BOILERPLATE_RE.search(value):
                return False
    return True
