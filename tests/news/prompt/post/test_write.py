from fetch import EXCERPT_CHARS
from news.follows import FEATURE_GAME, FEATURE_OTHER, FEATURE_TICKET
from news.prompt import LINE_RULE, TITLE_RULE
from news.prompt.post.write import WRITE_PROMPT


def test_write_prompts():
    assert set(WRITE_PROMPT) == {FEATURE_GAME, FEATURE_TICKET, FEATURE_OTHER}
    game = WRITE_PROMPT[FEATURE_GAME]
    ticket = WRITE_PROMPT[FEATURE_TICKET]
    other = WRITE_PROMPT[FEATURE_OTHER]
    assert LINE_RULE in game and TITLE_RULE in game
    assert '"title"' in game and '"time"' in game and '"reward"' in game
    assert '"about"' in game and '"detail"' in game
    assert LINE_RULE in ticket and TITLE_RULE in ticket
    assert '"shows"' in ticket and '"window"' in ticket and '"price"' in ticket
    assert TITLE_RULE in other and LINE_RULE in other
    assert '"summary"' in other
    assert str(EXCERPT_CHARS) in other
