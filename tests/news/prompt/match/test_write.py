from news.cards import MATCH_RECAP_CHARS
from news.prompt import LINE_RULE
from news.prompt.match.write import MATCH_WRITE_PROMPT


def test_match_write_prompt():
    assert LINE_RULE in MATCH_WRITE_PROMPT
    assert str(MATCH_RECAP_CHARS) in MATCH_WRITE_PROMPT
    assert '"title"' in MATCH_WRITE_PROMPT and '"summary"' in MATCH_WRITE_PROMPT
    assert "**Home**" in MATCH_WRITE_PROMPT and "**Away**" in MATCH_WRITE_PROMPT
    assert "quarters" in MATCH_WRITE_PROMPT
