from __future__ import annotations

from news.cards import (
    MATCH_RECAP_CHARS,
    TITLE_CHARS,
    apply_match_recap,
    embed_fingerprint,
    game_title,
    match_event_id,
    match_recap_notes,
    match_write_phase,
    news_embed,
    saved_match_summary,
)
from news.follows import FEATURE_GAME, FEATURE_MATCH, FEATURE_OTHER, FEATURE_TICKET
from news.source.post.common import CardPicture
from tests.helpers import (
    fetched_item,
)


def test_card_copy():
    case_news_embed_urls_breaks_and_recap()
    case_recap_notes_follow_full_time()

def case_news_embed_urls_breaks_and_recap():
    raw = news_embed(
        fetched_item(url="tag:example.com,2005:1", image_url="/relative.png"),
        FEATURE_GAME,
    )
    assert not raw.url
    assert raw.image is None or not raw.image.url
    assert raw.footer.text.startswith("Game Post")
    folded = news_embed(fetched_item(text="hello\n\nworld"), FEATURE_GAME)
    assert folded.description == "hello world"
    card = news_embed(
        fetched_item(title="raw", text="long original"),
        FEATURE_GAME,
        title="预下载开启",
        summary="**雪凇幽梦** 预下载已开\n9月2日强制更新。",
    )
    assert card.title == "预下载开启"
    assert card.description == "**雪凇幽梦** 预下载已开\n9月2日强制更新。"
    assert card.color is not None and card.color.value == 0x3DDCFF
    weibo = news_embed(fetched_item(platform="weibo"), FEATURE_GAME)
    assert weibo.color is not None and weibo.color.value == 0xC25454
    assert weibo.image is None or not weibo.image.url
    raw_title = "崩坏：星穹铁道 4.6版本「月升之前，与兽共舞」前瞻特别节目"
    assert game_title("崩坏星穹铁道", raw_title) == "崩坏星穹铁道 | 4.6版本前瞻特别节目"
    long_event = "女神异闻录 3 Reload 月行水上 限时活动即将开启"
    assert game_title("明日方舟", long_event) == f"明日方舟 | {long_event}"
    rail = news_embed(
        fetched_item(title=raw_title, source_name="崩坏星穹铁道"),
        FEATURE_GAME,
        title=raw_title,
    )
    assert rail.title == "崩坏星穹铁道 | 4.6版本前瞻特别节目"
    assert "|" in (rail.title or "")
    fallback = news_embed(
        fetched_item(title="raw", text="long original"),
        FEATURE_GAME,
        title="",
        summary="AI body",
    )
    assert fallback.title == "raw"
    assert fallback.description == "AI body"
    long_title = news_embed(
        fetched_item(title="n" * (TITLE_CHARS + 40)),
        FEATURE_GAME,
    )
    assert long_title.title == "n" * TITLE_CHARS
    long_music = news_embed(
        fetched_item(title="n" * (TITLE_CHARS + 40)),
        FEATURE_TICKET,
    )
    assert long_music.title is not None
    assert len(long_music.title) == TITLE_CHARS
    assert card.image.url == "https://example.test/cover.png"
    assert not card.author.name
    other = news_embed(
        fetched_item(
            source_name="Studio",
            author_icon="https://i0.hdslb.com/bfs/face/s.jpg",
        ),
        FEATURE_OTHER,
        title="夜航开放",
        summary="新地图今晚开放。\n活动持续到下周一。",
    )
    assert other.author.name == "Studio"
    assert other.author.icon_url == "https://i0.hdslb.com/bfs/face/s.jpg"
    assert not other.author.url
    assert other.title == "夜航开放"
    assert other.description == "新地图今晚开放。\n活动持续到下周一。"
    assert other.footer.text == "Other Post · Studio"
    same = news_embed(fetched_item(title="Hello", text="Hello"), FEATURE_OTHER)
    assert same.title == "Hello"
    assert same.description is None
    longer = news_embed(fetched_item(title="Hello", text="Hello world"), FEATURE_OTHER)
    assert longer.title == "Hello"
    assert longer.description == "Hello world"
    emoji = news_embed(
        fetched_item(
            title="⚰️",
            text="⚰️",
            source_name="宫叶Miyaba",
            url="https://www.bilibili.com/opus/1",
        ),
        FEATURE_OTHER,
    )
    assert emoji.title == "⚰️˙"
    assert emoji.description is None
    assert emoji.url == "https://www.bilibili.com/opus/1"
    assert emoji.author.name == "宫叶Miyaba"
    assert not emoji.author.url
    assert emoji.footer.text == "Other Post · 宫叶Miyaba"
    repeated = news_embed(
        fetched_item(title="★", text="★\n★", source_name="Studio"),
        FEATURE_OTHER,
    )
    assert repeated.title == "★˙"
    assert repeated.description is None
    assert not repeated.author.url
    kept = news_embed(
        fetched_item(title="⚰️", text="⚰️\n新图", source_name="Studio"),
        FEATURE_OTHER,
    )
    assert kept.title == "⚰️˙"
    assert kept.description == "⚰️ 新图"
    assert not kept.author.url
    weibo_other = news_embed(
        fetched_item(
            platform="weibo",
            source_name="Studio",
            author_icon="https://tvax1.sinaimg.cn/crop/s.jpg",
        ),
        FEATURE_OTHER,
        face=CardPicture(b"face", "face.jpg"),
    )
    assert weibo_other.author.name == "Studio"
    assert weibo_other.author.icon_url == "attachment://face.jpg"
    assert not weibo_other.author.url
    named = news_embed(
        fetched_item(platform="weibo", source_name="Studio"),
        FEATURE_OTHER,
    )
    assert named.author.name == "Studio"
    assert not named.author.icon_url
    broken = news_embed(
        fetched_item(source_name="Studio\nline", author_icon="http://"),
        FEATURE_OTHER,
    )
    assert broken.author.name == "Studio line"
    assert not broken.author.icon_url
    long_name = news_embed(
        fetched_item(source_name="名" * 300, author_icon="not-a-url"),
        FEATURE_OTHER,
    )
    assert long_name.author.name is not None
    assert len(long_name.author.name) == 256
    assert not long_name.author.icon_url
    secure = news_embed(
        fetched_item(
            source_name="Studio",
            author_icon="http://i0.hdslb.com/bfs/face/s.jpg",
        ),
        FEATURE_OTHER,
    )
    assert secure.author.icon_url == "https://i0.hdslb.com/bfs/face/s.jpg"
    tall = news_embed(
        fetched_item(image_width=1080, image_height=4000),
        FEATURE_GAME,
    )
    assert tall.image is None or not tall.image.url
    live_item = fetched_item(
        platform="thesportsdb",
        title="Arsenal vs Chelsea",
        text="**Live** | Q3\n**Venue** | Arena",
        tags=("match", "start"),
        source_name="Arsenal",
    )
    match = news_embed(live_item, FEATURE_MATCH)
    assert match.title == "Arsenal vs Chelsea"
    assert match.description == "**Live** | Q3\n**Venue** | Arena"
    assert match.footer.text == "Match · Arsenal"
    assert match.color is not None and match.color.value == 0x228B22
    linked = news_embed(
        fetched_item(
            platform="thesportsdb",
            url="https://www.espn.com/soccer/match/_/gameId/1",
            tags=("match", "end"),
        ),
        FEATURE_MATCH,
    )
    sportsdb = news_embed(
        fetched_item(
            platform="thesportsdb",
            url="https://www.thesportsdb.com/event/1-arsenal-vs-chelsea",
            tags=("match", "end"),
        ),
        FEATURE_MATCH,
    )
    assert embed_fingerprint(linked) != embed_fingerprint(sportsdb)
    end_notes = fetched_item(
        platform="thesportsdb",
        text="**Game Status** | Over\n**Home** | Arsenal\n**League** | Premier League\n\n**Details**\nSaka 12'\nHavertz 40'",
        tags=("match", "end"),
    )
    recap = news_embed(end_notes, FEATURE_MATCH, summary="Saka and Havertz scored.")
    assert recap.description.endswith("Saka and Havertz scored.")
    soccer = news_embed(
        end_notes,
        FEATURE_MATCH,
        summary="**Home**\nSaka 12'\nHavertz 40'\n\n**Away**\nPalmer 55'",
    )
    assert soccer.description is not None
    assert soccer.description.endswith(
        "**Home**\nSaka 12'\nHavertz 40'\n\n**Away**\nPalmer 55'"
    )
    long = "x" * (MATCH_RECAP_CHARS + 40)
    assert len(apply_match_recap(end_notes.text, long).splitlines()[-1]) <= MATCH_RECAP_CHARS
    full = news_embed(end_notes, FEATURE_MATCH, summary="Saka " + "x" * 300)
    assert full.description is not None
    assert "…" not in full.description


def case_recap_notes_follow_full_time():
    next_card = fetched_item(
        "1:next", platform="thesportsdb", tags=("match", "next"), occurred_at=None
    )
    end = fetched_item(
        "1:end",
        platform="thesportsdb",
        tags=("match", "end"),
        text=(
            "**Game Status** | Over\n**Home** | Arsenal\n"
            "**League** | Premier League\n\n**Details**\nSaka 12'\nHavertz 40'"
        ),
    )
    assert not match_write_phase(next_card)
    assert match_write_phase(end)
    assert match_recap_notes(end) == "Saka 12'\nHavertz 40'"
    assert match_event_id(end) == "1"
    notes = "Saka 12'\nHavertz 40'"
    assert saved_match_summary(
        {"stage": "end", "notes": notes, "summary": "Saka scored early."}, "end", notes
    ) == "Saka scored early."
    assert saved_match_summary(
        {"stage": "end", "notes": notes, "summary": "  "}, "end", notes
    ) is None
    assert saved_match_summary(
        {"stage": "start", "notes": notes, "summary": "Saka scored early."}, "end", notes
    ) is None
