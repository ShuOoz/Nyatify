from news.drafts import FILTER_FACTS, FILTER_TYPES
from news.follows import FEATURE_GAME, FEATURE_TICKET
from news.prompt.post.filter import FILTER_JSON, FILTER_PROMPT


def test_filter_prompts():
    assert set(FILTER_PROMPT) == {FEATURE_GAME, FEATURE_TICKET}
    for prompt in FILTER_PROMPT.values():
        assert FILTER_JSON in prompt
        assert "type drop if none" in prompt
        for key in FILTER_FACTS:
            assert key in prompt
    game = FILTER_PROMPT[FEATURE_GAME]
    ticket = FILTER_PROMPT[FEATURE_TICKET]
    for kind in FILTER_TYPES - {"notice", "release", "drop"}:
        assert kind in game or kind in ticket
    assert "ops" in game and "stream" in game and "collab_grant" in game
    assert "show" in ticket and "音乐节" in ticket
    assert "drop" in game and "drop" in ticket
