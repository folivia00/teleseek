"""
Multilingual search on self-contained synthetic rows: prefix fallback for
agglutinative languages and pg_trgm typo tolerance for channel names.

The test inserts its own channels/posts (username prefix ``mltest_``) and
removes them afterwards, so it does not depend on the demo data set.
"""

import pytest
from sqlalchemy import text

from bot.database.queries import search_channels, search_posts

TG_BASE = -100888109000
PFX = "mltest_"


@pytest.fixture
async def multilang_rows(session_factory):
    async def _cleanup(s):
        await s.execute(text(
            "DELETE FROM posts WHERE channel_id IN (SELECT id FROM channels WHERE username LIKE 'mltest_%')"))
        await s.execute(text("DELETE FROM channels WHERE username LIKE 'mltest_%'"))
        await s.commit()

    async with session_factory() as s:
        await _cleanup(s)
        # Channels: Uzbek (Latin script), a "typo target" Telegram channel, Kazakh.
        await s.execute(text(
            "INSERT INTO channels (telegram_id, title, username, description, type, is_active) VALUES "
            f"({TG_BASE - 1}, 'Toshkent yangiliklari', '{PFX}uz', 'Oxirgi yangiliklar har kuni', 'channel', TRUE), "
            f"({TG_BASE - 2}, 'Telegram Hub News',     '{PFX}tg', 'channel about telegram',      'channel', TRUE), "
            f"({TG_BASE - 3}, 'Қазақстан жаңалықтары', '{PFX}kz', 'Күнделікті жаңалықтар',       'channel', TRUE)"))
        ids = dict((await s.execute(text(
            "SELECT username, id FROM channels WHERE username LIKE 'mltest_%'"))).all())
        # Posts: Kazakh/Uzbek word forms + a Russian background set for the threshold case.
        rows = [
            (ids[PFX + "kz"], 1, "Бүгінгі жаңалықтар: Астанада жаңа мектеп ашылды"),
            (ids[PFX + "kz"], 2, "Соңғы жаңалық туралы толық ақпарат"),
            (ids[PFX + "uz"], 1, "Bugungi yangiliklar: Toshkentda yangi metro ochildi"),
            (ids[PFX + "uz"], 2, "Eng songgi yangilik haqida batafsil"),
            (ids[PFX + "tg"], 50, "Новостной портал запускается завтра"),
        ] + [(ids[PFX + "tg"], i, f"Свежие новости дня номер {i}") for i in range(3, 3 + 12)]
        for cid, mid, body in rows:
            await s.execute(text(
                "INSERT INTO posts (channel_id, telegram_message_id, text, views_count, post_date) "
                "VALUES (:c, :m, :t, 10, now())"), {"c": cid, "m": mid, "t": body})
        await s.commit()
    try:
        yield
    finally:
        async with session_factory() as s:
            await _cleanup(s)


async def test_kazakh_word_forms_via_prefix_fallback(session_factory, multilang_rows):
    async with session_factory() as s:
        found = await search_posts(s, "жаңалық", limit=11)
    assert len(found) >= 2


async def test_uzbek_word_forms_via_prefix_fallback(session_factory, multilang_rows):
    async with session_factory() as s:
        found = await search_posts(s, "yangilik", limit=11)
    assert len(found) >= 2


async def test_prefix_fallback_stays_off_when_exact_hits_suffice(session_factory, multilang_rows):
    # 12+ exact hits for "новости" (above the threshold of 10) → the fallback is off,
    # so "Новостной портал" (matched only by the prefix) must not be mixed in.
    async with session_factory() as s:
        found = await search_posts(s, "новости", limit=11)
    assert len(found) == 11
    assert not any("Новостной портал" in (p.text or "") for p in found)


async def test_typo_in_channel_name_found_by_trigrams(session_factory, multilang_rows):
    async with session_factory() as s:
        found = await search_channels(s, "teleram", limit=10)
    assert any(c.username == PFX + "tg" for c in found), [c.title for c in found]


async def test_uzbek_channel_name_word_form(session_factory, multilang_rows):
    async with session_factory() as s:
        found = await search_channels(s, "yangilik", limit=10)
    assert any(c.username == PFX + "uz" for c in found), [c.title for c in found]


async def test_exact_channel_name_uses_full_text_path(session_factory, multilang_rows):
    async with session_factory() as s:
        found = await search_channels(s, "telegram", limit=10)
    assert any(c.username == PFX + "tg" for c in found)
