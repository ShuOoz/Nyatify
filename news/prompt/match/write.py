"""Write prompt for a match recap."""

from news.cards import MATCH_RECAP_CHARS
from news.prompt import LINE_RULE

MATCH_WRITE_PROMPT = (
    "Format these notes as Discord details. "
    "JSON only: "
    '{"title":"","summary":"…"}. '
    f"summary is at most {MATCH_RECAP_CHARS} characters including newlines. "
    + LINE_RULE
    + "Keep title empty. Use only these notes. Do not invent names, minutes, or scores. "
    "Soccer: summary is only the goal list. Drop every other sentence. "
    "**Home**, then the home scorers, a blank line, then **Away**. "
    "Heading on its own line, no colon. "
    "One scorer and minute per line, copied as written. Empty side is -. Keep both headings. "
    "Other sports: copy the period box as-is "
    "(Lakers quarters: 19 23 24 22; Warriors quarters: 21 21 21 29). "
    "No hashtags, no URLs, or extra keys."
)
