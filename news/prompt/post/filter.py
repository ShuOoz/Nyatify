"""Filter prompts for Bilibili and Weibo posts."""

from news.follows import FEATURE_GAME, FEATURE_TICKET

FILTER_JSON = (
    "JSON only: "
    '{"why":"one beat","type":"ops","facts":{"down":"quote","how":"quote"}}. '
    "why first (scratch only), then type and facts. "
    "facts keys: until, down, how, grant, start. "
    "Each value is a short exact copy from the unique body, or omit. "
    "Do not invent or paraphrase. Ignore raffle and store-download stamps. "
)

FILTER_PROMPT = {
    FEATURE_GAME: (
        "Read the whole unique body. The title only names the post. "
        "A later sentence can be the keep. A recap or version-overview title does not cancel it. "
        "Quote that sentence, even under a recap title. "
        + FILTER_JSON
        + "why: first person, the sentence that decides. "
        "Keep only: long version patch (ops), a watch notice (stream), "
        "or an in-game prize claimed outside the client (collab_grant). "
        "ops = predownload, downtime, or a forced stop the client does not already show. "
        "until = date/window (维护后 is until, not down). "
        "down = 停服 / 停机 / N hours, not 在线更新. "
        "how = predownload, relogin after downtime, or the place outside the client. "
        "stream = that sentence is the watch notice, with the clock. "
        "A PV, a kit, or lore that only prints the time is not the notice. "
        "A 直播预约 line is the notice, 开启 or not. "
        "start = that clock: a time, and 前瞻/直播/开播/播出/节目 when the line "
        "says 开启. not 预售 / concert / 正式上线 / event 开启 with no watch words. "
        "A raffle on that notice is a stamp: type stream and quote start. "
        "collab_grant = the prize is in the game and the claim is outside the client. "
        "A code, a 认证, a survey, or a web activity is that claim. "
        "grant = that prize, not merch or stickers. "
        "how may be inside the grant quote. "
        "Login, tasks, or an event played inside the client is a farm: drop. "
        "A 创作者激励 / 投稿 bounty is not this claim: drop. "
        "Examples: 'predownload and a downtime window'; "
        "'a hotfix the client already shows'; "
        "'the show sentence has the clock, a raffle on it is a stamp'; "
        "'a PV, kit, or lore line only prints the time'; "
        "'a 直播预约 line is the notice'; "
        "'a recap title, a later line is the outside claim'; "
        "'a web activity pays an in-game prize'; "
        "'a banner the client already shows'. "
        "Else drop (gacha, hotfix, PV, kit, lore, in-game loops, concert, merch, "
        "创作者激励, 话). "
        "type drop if none."
    ),
    FEATURE_TICKET: (
        "Read the official music post. "
        + FILTER_JSON
        + "why: first person, one beat. "
        "Keep only a concert or livehouse ticket sale (show). "
        "A music festival (音乐节) is never show: drop it, sale or not. "
        "Need the purchase window, or is it only a notice that a show exists? "
        "show = the post says tickets are on sale or the sale is opening, "
        "and the event is a concert or a livehouse. "
        "start = the dates or the clock on that sale, copied from the post. "
        "how = how to buy or how to go, when the post says it. "
        "Any wording for that sale, with those dates, is the window. "
        "开票 is one such wording. "
        "type show for that sale. Not ops. "
        "A show day, a guest, or a venue that never says the sale is not show. "
        "The night before, with no sale and no way to buy, is not show. "
        "A 大麦 post sells tickets. When that post stops mid-sentence before the sale window, it is still show. "
        "Quote a date written in full. "
        "Drop a post whose subject is the concert rather than the sale. "
        "A day or a place in that post is not a window. "
        "Else drop other posts about the concert or livehouse "
        "(song, lineup, the show day, guest, merch, giveaways, rehearsal, recap, photos, thanks). "
        "type drop if none."
    ),
}
