"""
End-to-end search checks on the synthetic demo data set (scripts/seed_data.py).

They go through the same entry point as the bot (SearchEngine) and document,
as executable examples, what the search can and cannot do in each language.
Skipped when the demo data is not loaded.
"""

from sqlalchemy import text

from bot.database import queries
from bot.database.models import MediaType
from bot.services.search_engine import ContentType, SearchEngine


def _usernames(channels) -> set[str]:
    return {c.username for c in channels}


def _texts(posts) -> list[str]:
    return [p.text or "" for p in posts]


async def test_english_morphology(demo_data) -> None:
    # "recipe" matches "recipes" through the English Snowball stemmer.
    async with demo_data() as s:
        posts = await queries.search_posts(s, "recipe", limit=20)
    assert any("recipes" in t for t in _texts(posts))


async def test_russian_morphology(demo_data) -> None:
    # "рецепт" (singular) matches "рецептов"/"рецептами" through the Russian stemmer.
    async with demo_data() as s:
        posts = await queries.search_posts(s, "рецепт", limit=20)
    assert any("рецептов" in t for t in _texts(posts))


async def test_uzbek_prefix_fallback(demo_data) -> None:
    async with demo_data() as s:
        posts = await queries.search_posts(s, "yangilik", limit=20)
    assert any("yangiliklar" in t for t in _texts(posts))


async def test_kazakh_prefix_fallback(demo_data) -> None:
    async with demo_data() as s:
        posts = await queries.search_posts(s, "жаңалық", limit=20)
    assert any("жаңалықтар" in t for t in _texts(posts))


async def test_arabic_matches_exact_word_form_only(demo_data) -> None:
    # Without an Arabic stemmer, "وصفات" (recipes) and "الوصفات" (the recipes) are
    # different lexemes: each query finds its own form only.
    async with demo_data() as s:
        plain = _texts(await queries.search_posts(s, "وصفات", limit=20))
        with_article = _texts(await queries.search_posts(s, "الوصفات", limit=20))
    assert any("وصفات سهلة" in t for t in plain)
    assert not any("الوصفات التقليدية" in t for t in plain)
    assert any("الوصفات التقليدية" in t for t in with_article)


async def test_typo_tolerant_channel_lookup(demo_data) -> None:
    async with demo_data() as s:
        channels = await queries.search_channels(s, "tecnology digest", limit=10)
    assert "teleseek_demo_tech_digest" in _usernames(channels)


async def test_channel_found_by_topic_keywords(demo_data) -> None:
    # "Weekend Kitchen" never says "sourdough" in its title/description; its posts do,
    # so the channel is found through keywords built from its posts.
    async with demo_data() as s:
        channels = await queries.search_channels(s, "sourdough", limit=10)
    assert "teleseek_demo_weekend_kitchen" in _usernames(channels)


async def test_exact_duplicates_are_hidden(demo_data) -> None:
    async with demo_data() as s:
        posts = await queries.search_posts(s, "developer meetup", limit=20)
    meetup = [t for t in _texts(posts) if t.startswith("Join the online developer meetup")]
    assert len(meetup) == 1


async def test_media_filter_returns_only_requested_type(demo_data) -> None:
    async with demo_data() as s:
        videos = await queries.search_posts_with_media(s, "recipe", MediaType.video, limit=20)
        assert videos, "expected at least one recipe video"
        ids = [p.id for p in videos]
        with_video = (await s.execute(text(
            "SELECT count(DISTINCT post_id) FROM media WHERE type = 'video' AND post_id = ANY(:ids)"),
            {"ids": ids})).scalar_one()
    assert with_video == len(ids)


async def test_pagination_has_more_for_news(demo_data) -> None:
    async with demo_data() as s:
        engine = SearchEngine(s)
        page1 = await engine.search_by_type("news", ContentType.POSTS, page=1, per_page=10)
        page2 = await engine.search_by_type("news", ContentType.POSTS, page=2, per_page=10)
    assert len(page1.posts) == 10 and page1.has_more
    assert page2.posts
    assert not {p.id for p in page1.posts} & {p.id for p in page2.posts}


async def test_sort_by_views(demo_data) -> None:
    async with demo_data() as s:
        posts = await queries.search_posts(s, "news", limit=10, sort="v")
    views = [p.views_count for p in posts]
    assert views == sorted(views, reverse=True)


async def test_content_filter_hides_18plus_by_default(demo_data) -> None:
    async with demo_data() as s:
        safe = await queries.search_channels(s, "horror", limit=10, adult_only=False)
        adult = await queries.search_channels(s, "horror", limit=10, adult_only=True)
    assert "teleseek_demo_horror_cinema" not in _usernames(safe)
    assert "teleseek_demo_horror_cinema" in _usernames(adult)
