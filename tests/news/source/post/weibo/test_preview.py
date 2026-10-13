from __future__ import annotations

from config import settings as live_settings
from news.source.post.weibo.preview import _download, cover_url, fetch_cover, item_cover


async def test_weibo_covers(monkeypatch):
    case_cover_url_prefers_a_photo_then_a_video_cover()
    await case_fetch_cover_downloads_jpeg_with_weibo_referer()
    await case_fetch_cover_rejects_blocked_and_non_image()
    await case_item_cover_skips_other_platforms_and_missing_urls()
    await case_download_uses_proxy_only_when_set(monkeypatch)

def case_cover_url_prefers_a_photo_then_a_video_cover():
    found = cover_url(
        {
            "pics": [
                {"type": "video", "url": "https://wx1.sinaimg.cn/large/skip.jpg"},
                {"large": {"url": "//wx2.sinaimg.cn/mw2000/pic.jpg"}},
            ],
            "page_info": {
                "type": "video",
                "page_pic": {"url": "https://wx3.sinaimg.cn/orj480/cover.jpg"},
            },
        }
    )
    assert found == "https://wx2.sinaimg.cn/mw2000/pic.jpg"
    clip = cover_url(
        {
            "page_info": {
                "type": "video",
                "page_pic": "https://wx4.sinaimg.cn/orj480/cover.jpg",
            }
        }
    )
    assert clip == "https://wx4.sinaimg.cn/orj480/cover.jpg"
    topic = cover_url(
        {
            "page_info": {
                "type": "search_topic",
                "page_pic": {"url": "https://wx1.sinaimg.cn/large/topic.jpg"},
            }
        }
    )
    assert topic is None
    forwarded = cover_url(
        {
            "retweeted_status": {
                "pics": [{"url": "https://wx1.sinaimg.cn/large/orig.jpg"}]
            }
        }
    )
    assert forwarded == "https://wx1.sinaimg.cn/large/orig.jpg"
    assert cover_url({"pics": [{"url": "https://example.test/pic.jpg"}]}) is None


async def case_fetch_cover_downloads_jpeg_with_weibo_referer():
    seen = {}

    async def call(url, headers):
        seen["url"] = url
        seen["headers"] = headers
        return 200, "image/jpeg", b"\xff\xd8\xff"

    cover = await fetch_cover("https://wx1.sinaimg.cn/large/pic.jpg", call=call)
    assert cover is not None
    assert cover.data == b"\xff\xd8\xff"
    assert cover.filename == "cover.jpg"
    assert seen["url"] == "https://wx1.sinaimg.cn/large/pic.jpg"
    assert seen["headers"]["Referer"] == "https://weibo.com/"


async def case_fetch_cover_rejects_blocked_and_non_image():
    async def html(_url, _headers):
        return 403, "text/html", b"<html>"

    assert await fetch_cover("https://wx1.sinaimg.cn/large/pic.jpg", call=html) is None
    assert await fetch_cover("https://example.test/pic.jpg", call=html) is None

    async def huge(_url, _headers):
        return 200, "image/png", b"x" * (2_000_001)

    assert await fetch_cover("https://wx1.sinaimg.cn/large/pic.jpg", call=huge) is None


async def case_item_cover_skips_other_platforms_and_missing_urls():
    class _Item:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    assert (
        await item_cover(
            _Item(platform="bilibili", image_url="https://wx1.sinaimg.cn/large/pic.jpg")
        )
        is None
    )
    assert await item_cover(_Item(platform="weibo", image_url=None)) is None
    assert await item_cover(_Item(platform="weibo", image_url="https://example.test/pic.jpg")) is None


async def case_download_uses_proxy_only_when_set(monkeypatch):
    sent = []

    class _Response:
        status = 200
        headers = {"Content-Type": "image/jpeg"}

        async def read(self):
            return b"\xff\xd8\xff"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    class _Session:
        def get(self, url, **kwargs):
            sent.append(kwargs)
            return _Response()

    async def session():
        return _Session()

    monkeypatch.setattr("news.source.post.weibo.preview._weibo_session", session)
    monkeypatch.setattr(live_settings, "weibo_proxy", None)
    await _download("https://wx1.sinaimg.cn/large/pic.jpg", {"Referer": "https://weibo.com/"})
    monkeypatch.setattr(live_settings, "weibo_proxy", "http://127.0.0.1:7890")
    await _download("https://wx1.sinaimg.cn/large/pic.jpg", {"Referer": "https://weibo.com/"})
    assert "proxy" not in sent[0]
    assert sent[1]["proxy"] == "http://127.0.0.1:7890"
