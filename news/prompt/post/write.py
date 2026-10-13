"""Write prompts for Bilibili and Weibo posts."""

from fetch import EXCERPT_CHARS
from news.follows import FEATURE_GAME, FEATURE_TICKET, FEATURE_OTHER
from news.prompt import LINE_RULE, TITLE_RULE

WRITE_PROMPT = {
    FEATURE_GAME: (
        "Write a game-news Discord embed from this post. "
        "JSON only: "
        '{"title":"","time":"","about":"","detail":"","reward":""}. '
        "Say each fact once in the sentences. Keep the post's language. Use only this post. "
        "Do not invent times, names, or amounts. "
        + LINE_RULE
        + "A label names one thing in a few words. "
        "Under it, write one sentence only when the label left something out. "
        "Add a second sentence on the next line only for a second fact. "
        "When there is nothing more to say, leave those sentences out. "
        "title is one short line: version name or number, then the kind of notice. "
        "The card shows Account | title, and that whole line is the title. "
        + TITLE_RULE
        + "A day is fine. No account name, no slogan, no 「quotes」, no clock, no reward. "
        "time is the notice clock in full: date and time, and the start–end window when the post has one. "
        "Empty when the post has no notice clock. "
        "A clock that belongs to a reward stays with the reward. "
        "about is the kind in a few words, not the title copied again, not a list. "
        "detail is at most two sentences: the subject and what changed. "
        "Never a list of events. If the post has many items, keep the lead and leave the rest out. "
        "Leave out anything title, time, or reward already says. "
        "Empty when there is nothing more to say. "
        "reward is one line naming one claimable grant, written as the post states it. "
        "Empty when there is nothing to get. No raffle. "
        "Empty for in-game shops, 签到, mail, furniture, banners, and 上架. "
        "Under that line, one sentence about the grant only when the post says where to get it or when it ends. "
        "A second sentence on the next line only for a second fact about the grant. "
        "That sentence does not repeat the grant line, and does not start with it. "
        "When the post has nothing more about the grant, the reward is only the grant line. "
        "Heading-only post: title as above; about is that heading; time, detail, and reward empty. "
        "No headings in the JSON, no extra keys."
    ),
    FEATURE_TICKET: (
        "Write a music-news Discord embed from this post. "
        "JSON only: "
        '{"title":"","about":"","window":"","where":"",'
        '"shows":[{"performer":"","date":"","place":"","price":""}],"price":""}. '
        "Keep the post's language. Use only this post. "
        "Do not invent times, names, or amounts. "
        "Copy performer, date, and place from the post. "
        "When the post stops mid-sentence, keep every fact that is fully written. "
        "Leave the unfinished fragment out. Do not finish it. "
        + LINE_RULE
        + "title is Artist | Event. "
        "Artist is the performer. Event names the concert, festival, or livehouse. "
        + TITLE_RULE
        + "No account name, no slogan, no clock, no city. "
        "about is one or two sentences, not a list, and not a show fact. "
        "Empty when there is nothing more to say. "
        "Do not write labels in the JSON. An empty field stays empty. "
        "window is the sale clock only. where is where to buy, in a few words. "
        "One show per venue. date is that venue's show days, copied as the post writes them. "
        "A later day that leaves out the month stays that way. Do not fill the month in. "
        "Keep every show the post states. "
        "Price is the amounts, separated by slashes, not on the date. "
        "Keep a name the post gives that price. "
        "Prices every show shares go in price, and each show price stays empty. "
        "A price only one show has goes on that show, and price stays empty. "
        "No hashtags, no URLs, no extra keys."
    ),
    FEATURE_OTHER: (
        "Write an other-news Discord embed from this post. "
        "JSON only: "
        '{"title":"one line","summary":"card body"}. '
        f"summary is at most {EXCERPT_CHARS} characters including newlines. "
        "Keep the post's language. Use only this post. "
        "Do not invent times, names, or amounts. "
        "title is one short clear line that names the post. "
        + TITLE_RULE
        + "No account name, no slogan. "
        "The post is often short, sometimes only a heading, sometimes empty. "
        "When there is no body, that heading is the title "
        "and summary is that same line. "
        "summary keeps the post's words. Reformat only. "
        "Join a sentence split across lines into one line. "
        + LINE_RULE
        + "No hashtags, no URLs, no extra keys."
    ),
}
