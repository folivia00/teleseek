"""
Synthetic demo data for TeleSeek.

Everything here is invented: channel names, usernames (all prefixed with
``teleseek_demo_``), posts, users and queries. Telegram ids are placed in
ranges that real accounts and channels do not use. There is no real channel,
person or brand in this file.

The data set is small but deliberately shaped to exercise every search path:
  * English and Russian morphology  — "recipe" finds "recipes", "рецепт" finds "рецептов";
  * Uzbek / Kazakh prefix fallback  — "yangilik" finds "yangiliklar", "жаңалық" finds "жаңалықтар";
  * Arabic exact-form matching      — "وصفات" and "الوصفات" are different lexemes;
  * typo-tolerant channel lookup    — "tecnology digest" finds "Technology Digest" (pg_trgm);
  * topic keywords                  — "sourdough" finds a channel whose title/description never say it;
  * hidden exact duplicates         — the same announcement reposted in two channels is shown once;
  * content filters and sorting     — photos/videos/audio/documents, views and dates vary;
  * pagination                      — "news" returns more than one page;
  * content filter (18+ flag)       — one channel (horror film reviews, no explicit content)
                                      carries the 18+ flag, so safe search hides it by default.

It also fills the analytics tables (search_log, user_stats, feedback,
channel_queue) so the monitoring panel has something to show.

Usage:
    python -m scripts.seed_data            # seeds only if the database has no channels
    SEED_CONFIRM=yes python -m scripts.seed_data --reset
                                           # wipes ALL application data, then seeds

The reset mode is destructive and therefore requires SEED_CONFIRM=yes.
DATABASE_URL is read from the environment (the bot settings are not imported,
so no bot token is needed).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

NOW = datetime.now(timezone.utc).replace(microsecond=0)
RNG = random.Random(42)  # deterministic demo data

# Telegram ids outside the ranges used by real channels/users.
CHANNEL_ID_BASE = -1009990000000
USER_ID_BASE = 990000000000

# ─── Channels ────────────────────────────────────────────────────────────────
# (username suffix, title, description, type, category, section, members, is_adult)
CHANNELS: list[tuple[str, str, str, str, str, str, int, bool | None]] = [
    # English
    ("tech_digest", "Technology Digest", "Daily technology news, gadget reviews and software releases.",
     "channel", "Technology", "International", 48200, None),
    ("world_news", "World News Brief", "Short news summaries from around the world, every morning.",
     "channel", "News", "International", 91500, None),
    ("home_cooking", "Home Cooking Club", "Simple recipes for every day: soups, pasta, baking and desserts.",
     "channel", "Cooking", "International", 23400, None),
    ("weekend_kitchen", "Weekend Kitchen", "Notes from our weekend experiments.",
     "channel", "Cooking", "International", 3100, None),
    ("running_fit", "Running & Fitness", "Training plans, running tips and race reports.",
     "channel", "Sports", "International", 15800, None),
    ("travel_notes", "Travel Notes", "Itineraries, budget travel tips and photo stories.",
     "channel", "Travel", "International", 27600, None),
    ("indie_music", "Indie Music Weekly", "New indie releases, playlists and concert announcements.",
     "channel", "Music", "International", 12900, None),
    ("startup_daily", "Startup Daily", "Founders, funding rounds and product launches.",
     "channel", "Business", "International", 19700, None),
    ("horror_cinema", "Horror Cinema Reviews", "Reviews of classic and new horror films. Mature themes.",
     "channel", "Cinema", "International", 8800, True),
    ("python_chat", "Python Developers Chat", "Community chat for Python developers: questions, code review, jobs.",
     "group", "Technology", "International", 5400, None),
    ("travel_buddies", "Travel Buddies", "Find travel companions and share trip plans.",
     "group", "Travel", "International", 2300, None),
    # Russian
    ("novosti_dnya", "Новости дня", "Главные новости дня коротко и по делу.",
     "channel", "News", "CIS", 64300, None),
    ("domashnie_recepty", "Домашние рецепты", "Простые рецепты на каждый день: супы, выпечка, десерты.",
     "channel", "Cooking", "CIS", 38100, None),
    ("tech_gadgets_ru", "Технологии и гаджеты", "Обзоры гаджетов, новости технологий и полезные программы.",
     "channel", "Technology", "CIS", 21700, None),
    ("beg_i_zdorovie", "Бег и здоровье", "Программы тренировок, советы по бегу и восстановлению.",
     "channel", "Sports", "CIS", 9600, None),
    ("kino_chat_ru", "Чат любителей кино", "Обсуждаем новые фильмы и сериалы.",
     "group", "Cinema", "CIS", 4100, None),
    # Uzbek (Latin script)
    ("kun_yangiliklari", "Kun yangiliklari", "Eng muhim yangiliklar qisqa va tushunarli.",
     "channel", "News", "Central Asia", 31200, None),
    ("oshxona_retseptlari", "Oshxona retseptlari", "Milliy taomlar va oson retseptlar har kuni.",
     "channel", "Cooking", "Central Asia", 17400, None),
    ("sport_olami_uz", "Sport olami", "Futbol, boks va boshqa sport turlari bo'yicha xabarlar.",
     "channel", "Sports", "Central Asia", 11300, None),
    # Kazakh (Cyrillic script)
    ("kz_zhanalyktar", "Күнделікті жаңалықтар", "Ең маңызды жаңалықтар қысқа әрі нақты.",
     "channel", "News", "Central Asia", 22800, None),
    ("kz_askhana", "Қазақ асханасы", "Ұлттық тағамдар мен қарапайым рецепттер.",
     "channel", "Cooking", "Central Asia", 9900, None),
    # Arabic
    ("akhbar_alyawm", "أخبار اليوم", "أهم الأخبار اليومية باختصار.",
     "channel", "News", "Middle East", 41700, None),
    ("matbakh_albayt", "مطبخ البيت", "وصفات سهلة للطبخ في البيت.",
     "channel", "Cooking", "Middle East", 26500, None),
    ("tiqniya_arabia", "تقنية اليوم", "مراجعات الهواتف وأخبار التقنية.",
     "channel", "Technology", "Middle East", 14200, None),
]

# ─── Posts ───────────────────────────────────────────────────────────────────
# Each entry: (text, media) where media is None or (type, file_name_or_None, size, duration).
P = tuple[str, tuple[str, str | None, int, int | None] | None]

POSTS: dict[str, list[P]] = {
    "tech_digest": [
        ("New laptop review: the lightweight model finally gets a better keyboard and a brighter screen.",
         ("photo", None, 245_000, None)),
        ("Software release notes: the open-source editor adds a plugin marketplace and faster search.", None),
        ("Video: we tested five wireless headphones for battery life and noise cancelling.",
         ("video", "headphones_test.mp4", 48_000_000, 412)),
        ("Smartphone camera comparison — night photos from three flagship phones.",
         ("photo", None, 312_000, None)),
        ("Download the PDF cheat sheet with keyboard shortcuts for developers.",
         ("document", "shortcuts_cheatsheet.pdf", 820_000, None)),
        ("Technology news: chip makers announce new energy-efficient processors for laptops.", None),
    ],
    "world_news": [
        ("Morning news: city council approves a new public transport plan.", None),
        ("News in brief: a new science museum opens downtown this weekend.",
         ("photo", None, 198_000, None)),
        ("Weather news: a warm and sunny week is expected across the region.", None),
        ("Economy news: small businesses report steady growth this quarter.", None),
        ("Evening news podcast: the main stories of the day in ten minutes.",
         ("audio", "evening_news.mp3", 9_600_000, 600)),
        ("Sports news: the local team wins the regional cup final.", None),
        ("Culture news: the annual book festival announces its programme.", None),
        ("Health news: doctors recommend more walking and less screen time.", None),
        ("Science news: researchers publish a new study about ocean currents.", None),
        ("Transport news: the airport adds new direct routes for the summer season.", None),
        ("Education news: universities open applications for online courses.", None),
        ("Technology news: the city launches free public Wi-Fi in parks.", None),
    ],
    "home_cooking": [
        ("Five easy recipes for cooking pasta at home in under twenty minutes.",
         ("photo", None, 210_000, None)),
        ("Our best soup recipe: tomato soup with roasted garlic and basil.", None),
        ("Baking day! A step-by-step video recipe for a classic apple pie.",
         ("video", "apple_pie.mp4", 61_000_000, 735)),
        ("Cooking tip: let the dough rest for thirty minutes before rolling it out.", None),
        ("Dessert recipes for the weekend: chocolate mousse and lemon tart.", None),
    ],
    "weekend_kitchen": [
        ("This weekend we baked sourdough bread with a crispy crust and an open crumb.",
         ("photo", None, 260_000, None)),
        ("Feeding the sourdough starter: flour, water and patience. Day three looks bubbly.", None),
        ("Sourdough pancakes are the best way to use leftover starter.", None),
        ("Our sourdough schedule: mix on Friday evening, shape on Saturday, bake on Sunday.", None),
    ],
    "running_fit": [
        ("Training plan for your first 10K: three runs per week for eight weeks.", None),
        ("Running tips: start slow, keep a steady pace and focus on breathing.", None),
        ("Race report from the city half marathon — a new personal best!",
         ("photo", None, 175_000, None)),
        ("Stretching routine after a long run, with a short video guide.",
         ("video", "stretching.mp4", 22_000_000, 300)),
    ],
    "travel_notes": [
        ("Budget travel tips: how to plan a week-long trip without overspending.", None),
        ("Travel photo story: mountain villages, old bridges and quiet lakes.",
         ("photo", None, 402_000, None)),
        ("Itinerary for three days in a coastal town: beaches, markets and seafood.", None),
        ("Download our printable packing list for a weekend trip.",
         ("document", "packing_list.pdf", 150_000, None)),
    ],
    "indie_music": [
        ("New release: a dreamy indie pop album recorded in a small home studio.",
         ("audio", "album_preview.mp3", 7_200_000, 214)),
        ("Playlist of the week: twenty calm songs for focus and reading.", None),
        ("Concert announcement: three indie bands play the open-air stage next month.", None),
    ],
    "startup_daily": [
        ("Funding news: a logistics startup raises its first seed round.", None),
        ("Product launch: a note-taking app adds offline sync and shared folders.", None),
        ("Founder interview: lessons learned from the first hundred customers.",
         ("video", "founder_interview.mp4", 88_000_000, 1210)),
        # Exact duplicate of a post in "tech_digest": demonstrates hidden duplicates.
        ("Join the online developer meetup this Thursday: talks about search engines and databases.", None),
    ],
    "horror_cinema": [
        ("Review: a slow-burn haunted house film with great sound design.", None),
        ("Top ten classic horror films to watch on a rainy evening.", None),
    ],
    "python_chat": [
        ("How do I write async database code with SQLAlchemy 2.0? Any good examples?", None),
        ("Code review request: a small FastAPI service with PostgreSQL full-text search.", None),
        ("Job post: remote Python developer, async experience preferred.", None),
    ],
    "travel_buddies": [
        ("Looking for travel companions for a hiking trip in the mountains next month.", None),
    ],
    "novosti_dnya": [
        ("Главные новости утра: в городе открылась новая линия трамвая.", None),
        ("Новости культуры: в музее открылась выставка современной живописи.",
         ("photo", None, 230_000, None)),
        ("Новости погоды: на выходных ожидается солнечная и тёплая погода.", None),
        ("Новости спорта: местная команда вышла в финал кубка.", None),
        ("Новости науки: учёные представили новый метод очистки воды.", None),
    ],
    "domashnie_recepty": [
        ("Пять простых рецептов супов на каждый день.", None),
        ("Подборка рецептов выпечки: пирог с яблоками и сырники.",
         ("photo", None, 205_000, None)),
        ("Видеорецепт: как приготовить домашнюю пасту без специальной машинки.",
         ("video", "pasta_ru.mp4", 54_000_000, 640)),
        ("Делимся любимыми рецептами десертов к чаю.", None),
    ],
    "tech_gadgets_ru": [
        ("Обзор нового смартфона: камера стала лучше, а батарея держит два дня.",
         ("photo", None, 280_000, None)),
        ("Полезные программы для работы с документами и таблицами.",
         ("document", "programmy_obzor.pdf", 640_000, None)),
        ("Новости технологий: производители представили энергоэффективные процессоры.", None),
    ],
    "beg_i_zdorovie": [
        ("Программа тренировок для начинающих бегунов: три пробежки в неделю.", None),
        ("Советы по бегу зимой: правильная одежда и разминка.", None),
    ],
    "kino_chat_ru": [
        ("Кто уже смотрел новый фильм? Делитесь впечатлениями без спойлеров.", None),
    ],
    "kun_yangiliklari": [
        ("Bugungi yangiliklar: shaharda yangi park ochildi.", ("photo", None, 190_000, None)),
        ("Iqtisodiy yangiliklar: kichik biznes uchun yangi imtiyozlar e'lon qilindi.", None),
        ("Sport yangiliklari: milliy jamoa navbatdagi o'yinda g'alaba qozondi.", None),
        ("Ob-havo yangiliklari: dam olish kunlari havo issiq bo'ladi.", None),
    ],
    "oshxona_retseptlari": [
        ("Palov retsepti: an'anaviy usulda tayyorlash bosqichma-bosqich.",
         ("video", "palov.mp4", 70_000_000, 900)),
        ("Oson retseptlar: somsa va lag'mon uyda tayyorlash.", None),
    ],
    "sport_olami_uz": [
        ("Futbol yangiliklari: chempionatda yangi mavsum boshlandi.", None),
    ],
    "kz_zhanalyktar": [
        ("Бүгінгі жаңалықтар: қалада жаңа саябақ ашылды.", ("photo", None, 185_000, None)),
        ("Экономикалық жаңалықтар: шағын бизнеске жаңа жеңілдіктер жарияланды.", None),
        ("Спорт жаңалықтары: ұлттық құрама кезекті ойында жеңіске жетті.", None),
    ],
    "kz_askhana": [
        ("Бешбармақ рецепті: дәстүрлі әдіспен дайындау.", None),
        ("Қарапайым рецепттер: бауырсақ пен қуырдақ үйде.", ("photo", None, 199_000, None)),
    ],
    "akhbar_alyawm": [
        ("أخبار اليوم: افتتاح حديقة جديدة في وسط المدينة.", ("photo", None, 201_000, None)),
        ("أخبار الاقتصاد: نمو مستقر للشركات هذا العام.", None),
        ("أخبار الرياضة: الفريق المحلي يفوز بالمباراة النهائية.", None),
    ],
    "matbakh_albayt": [
        # "وصفات" (recipes) — matched by the query "وصفات".
        ("وصفات سهلة للعشاء في عشرين دقيقة.", ("photo", None, 222_000, None)),
        # "الوصفات" (THE recipes, with the article) — a different lexeme without a stemmer.
        ("الوصفات التقليدية للحلويات في المناسبات.", None),
        ("فيديو: طريقة تحضير الخبز في البيت.", ("video", "khubz.mp4", 39_000_000, 480)),
    ],
    "tiqniya_arabia": [
        ("مراجعة هاتف جديد: كاميرا أفضل وبطارية تدوم يومين.", None),
        ("أخبار التقنية: معالجات جديدة موفرة للطاقة.", None),
    ],
}
# Duplicate announcement (same text in two channels) — hidden by exact-duplicate dedup.
POSTS["tech_digest"].append(
    ("Join the online developer meetup this Thursday: talks about search engines and databases.", None)
)

# ─── Analytics (panel demo) ──────────────────────────────────────────────────
DEMO_QUERIES: list[tuple[str, bool]] = [
    ("news", True), ("recipe", True), ("technology", True), ("running", True),
    ("travel", True), ("python", True), ("новости", True), ("рецепты", True),
    ("yangiliklar", True), ("жаңалықтар", True), ("أخبار", True), ("وصفات", True),
    ("sourdough", True), ("tecnology digest", True), ("indie music", True),
    ("quantum gardening", False), ("vintage typewriters", False), ("кружевные зонты", False),
]
FEEDBACK: list[tuple[str, str, bool]] = [
    ("Great bot! Could you add search by date range?", "en", True),
    ("Please add more cooking channels.", "en", False),
    ("Спасибо, поиск работает быстро.", "ru", False),
]
QUEUE_STATUSES = ["done_depth"] * 7 + ["in_progress", "new", "error"]


async def _app_tables(conn: AsyncConnection) -> list[str]:
    rows = await conn.execute(text(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "AND tablename NOT IN ('alembic_version', 'app_settings')"
    ))
    return [r[0] for r in rows]


async def reset(conn: AsyncConnection) -> None:
    tables = await _app_tables(conn)
    await conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    print(f"reset: truncated {len(tables)} tables")


async def seed(conn: AsyncConnection) -> None:
    channel_ids: dict[str, int] = {}
    for i, (suffix, title, desc, ctype, cat, section, members, adult) in enumerate(CHANNELS, start=1):
        username = f"teleseek_demo_{suffix}"
        row = await conn.execute(text(
            "INSERT INTO channels (telegram_id, title, username, description, type, members_count, "
            "invite_link, category, section, is_active, is_adult, source, created_at, updated_at) "
            "VALUES (:tid, :title, :username, :desc, CAST(:ctype AS channel_type), :members, "
            ":link, :cat, :section, true, :adult, 'dispatcher', :ts, :ts) RETURNING id"
        ), {
            "tid": CHANNEL_ID_BASE - i, "title": title, "username": username, "desc": desc,
            "ctype": ctype, "members": members, "link": f"https://t.me/{username}",
            "cat": cat, "section": section, "adult": adult, "ts": NOW - timedelta(days=30),
        })
        channel_ids[suffix] = row.scalar_one()

    n_posts = n_media = 0
    for suffix, posts in POSTS.items():
        cid = channel_ids[suffix]
        for msg_id, (body, media) in enumerate(posts, start=1):
            posted = NOW - timedelta(days=RNG.randint(0, 13), hours=RNG.randint(0, 23),
                                     minutes=RNG.randint(0, 59))
            row = await conn.execute(text(
                "INSERT INTO posts (channel_id, telegram_message_id, text, views_count, forwards_count, "
                "post_date, source, created_at) VALUES (:cid, :mid, :text, :views, :fwd, :pd, "
                "'dispatcher', :pd) RETURNING id"
            ), {
                "cid": cid, "mid": msg_id, "text": body, "views": RNG.randint(150, 25_000),
                "fwd": RNG.randint(0, 400), "pd": posted,
            })
            post_id = row.scalar_one()
            n_posts += 1
            if media:
                mtype, fname, size, duration = media
                await conn.execute(text(
                    "INSERT INTO media (post_id, type, file_id, file_name, file_size, duration, caption) "
                    "VALUES (:pid, CAST(:t AS media_type), :fid, :fn, :sz, :dur, :cap)"
                ), {
                    "pid": post_id, "t": mtype, "fid": f"demo-file-{post_id}-{mtype}",
                    "fn": fname, "sz": size, "dur": duration, "cap": body[:120],
                })
                n_media += 1

    # Ingestion queue rows (the panel's coverage view).
    for i, (suffix, title, _desc, _ct, cat, section, _m, _a) in enumerate(CHANNELS):
        status = QUEUE_STATUSES[i % len(QUEUE_STATUSES)]
        await conn.execute(text(
            "INSERT INTO channel_queue (username, name, url, category, section, channel_id, "
            "posts_parsed_count, parse_status, last_parsed_at, last_error, priority) "
            "VALUES (:u, :n, :url, :cat, :sec, :cid, :cnt, CAST(:st AS parse_status), :ts, :err, 0)"
        ), {
            "u": f"teleseek_demo_{suffix}", "n": title, "url": f"https://t.me/teleseek_demo_{suffix}",
            "cat": cat, "sec": section, "cid": channel_ids[suffix],
            "cnt": len(POSTS.get(suffix, [])), "st": status,
            "ts": NOW - timedelta(hours=RNG.randint(1, 48)),
            "err": "demo: channel unavailable" if status == "error" else None,
        })

    # Users, search log and feedback (synthetic ids).
    n_users = 12
    for u in range(1, n_users + 1):
        uid = USER_ID_BASE + u
        first = NOW - timedelta(days=RNG.randint(1, 13))
        await conn.execute(text(
            "INSERT INTO user_stats (telegram_user_id, username, language, requests_count, first_seen, last_seen) "
            "VALUES (:uid, NULL, :lang, :req, :first, :last)"
        ), {"uid": uid, "lang": RNG.choice(["en", "en", "ru"]), "req": RNG.randint(3, 40),
            "first": first, "last": NOW - timedelta(hours=RNG.randint(0, 20))})
    n_searches = 0
    for day in range(14):
        for _ in range(RNG.randint(6, 14)):
            query, has = RNG.choice(DEMO_QUERIES)
            await conn.execute(text(
                "INSERT INTO search_log (telegram_user_id, query, results_count, has_results, adult_only, created_at) "
                "VALUES (:uid, :q, :n, :has, false, :ts)"
            ), {"uid": USER_ID_BASE + RNG.randint(1, n_users), "q": query,
                "n": RNG.randint(1, 10) if has else 0, "has": has,
                "ts": NOW - timedelta(days=day, hours=RNG.randint(0, 23))})
            n_searches += 1
    for k, (body, lang, fav) in enumerate(FEEDBACK, start=1):
        await conn.execute(text(
            "INSERT INTO feedback (telegram_user_id, text, language, is_favorite, created_at) "
            "VALUES (:uid, :t, :lang, :fav, :ts)"
        ), {"uid": USER_ID_BASE + k, "t": body, "lang": lang, "fav": fav,
            "ts": NOW - timedelta(days=k)})

    print(f"seeded: {len(CHANNELS)} channels/groups, {n_posts} posts, {n_media} media, "
          f"{n_users} users, {n_searches} searches, {len(FEEDBACK)} feedback messages")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Load synthetic TeleSeek demo data.")
    parser.add_argument("--reset", action="store_true",
                        help="wipe ALL application data first (requires SEED_CONFIRM=yes)")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    if args.reset and os.environ.get("SEED_CONFIRM", "") != "yes":
        print("--reset wipes all data; set SEED_CONFIRM=yes to confirm", file=sys.stderr)
        return 2

    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            if args.reset:
                await reset(conn)
            existing = (await conn.execute(text("SELECT count(*) FROM channels"))).scalar_one()
            if existing:
                print(f"seed skipped: database already has {existing} channels (use --reset)")
                return 0
            await seed(conn)
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
