"""Model prompts, grouped by post and match."""

from news.cards import LINE_WIDTH, TITLE_WIDTH

LINE_RULE = (
    f"Every line fits in {LINE_WIDTH // 2} Chinese characters"
    f" (or {LINE_WIDTH} letters). "
    "Rewrite each sentence so it ends naturally within that limit. "
    "One sentence per line. Do not split a sentence across lines. "
)

TITLE_RULE = (
    f"That line fits in {TITLE_WIDTH // 2} Chinese characters"
    f" (or {TITLE_WIDTH} letters). "
    "Rewrite it so it ends naturally within that limit. "
)

from news.prompt.match import MATCH_WRITE_PROMPT
from news.prompt.post import FILTER_PROMPT, WRITE_PROMPT

__all__ = [
    "FILTER_PROMPT",
    "LINE_RULE",
    "MATCH_WRITE_PROMPT",
    "TITLE_RULE",
    "WRITE_PROMPT",
]
