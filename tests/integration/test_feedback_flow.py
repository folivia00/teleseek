"""
Functional test of the "Feedback" flow: handler → database, with fake aiogram
objects (Message/FSMContext) and a real database session. Everything the test
writes is rolled back.
"""

from sqlalchemy import text

from bot import i18n
from bot.handlers.feedback import (
    FeedbackStates,
    handle_feedback_button,
    handle_feedback_text,
)

TEST_UID = 999000222000


class FakeUser:
    """Minimal from_user."""
    id = TEST_UID
    username = "test_user"
    is_bot = False
    language_code = "ru"


class FakeMessage:
    """Message: text + recorder of replies."""

    def __init__(self, txt: str) -> None:
        self.text = txt
        self.from_user = FakeUser()
        self.answers: list[str] = []

    async def answer(self, text: str, **kw):  # noqa: ANN001
        self.answers.append(text)
        return self


class FakeState:
    """Minimal FSMContext: set_state/get_state/get_data/update_data."""

    def __init__(self) -> None:
        self._st = None
        self._data: dict = {}

    async def set_state(self, st=None):  # noqa: ANN001
        self._st = st

    async def get_state(self):
        return self._st

    async def get_data(self) -> dict:
        return dict(self._data)

    async def update_data(self, **kw):
        self._data.update(kw)


async def test_feedback_flow(session_factory) -> None:
    async with session_factory() as session:
        # 1) Button → prompt + waiting state (the Russian UI label is recognised).
        state = FakeState()
        m1 = FakeMessage(i18n.t("btn_feedback", "ru"))
        await handle_feedback_button(m1, session, state, lang="ru")
        assert m1.answers and m1.answers[0] == i18n.t("feedback_prompt", "ru")
        assert await state.get_state() == FeedbackStates.waiting_text

        # 2) Text → saved + thanks + state cleared.
        m2 = FakeMessage("Great bot, please add search by date!")
        await handle_feedback_text(m2, session, state, lang="ru")
        assert m2.answers and m2.answers[0] == i18n.t("feedback_thanks", "ru")
        assert await state.get_state() is None
        row = (await session.execute(text(
            "SELECT text, language, is_favorite FROM feedback WHERE telegram_user_id=:u"),
            {"u": TEST_UID})).first()
        assert row is not None and row[0].startswith("Great bot") and row[1] == "ru" and row[2] is False

        # 3) Rate limit 1/hour: a repeated button press (the English label is recognised too).
        m3 = FakeMessage(i18n.t("btn_feedback", "en"))
        await handle_feedback_button(m3, session, state, lang="ru")
        assert m3.answers and m3.answers[0] == i18n.t("feedback_rate_limited", "ru")
        assert await state.get_state() is None

        # 4) Rate limit on the write itself (bypass via a stuck state).
        await state.set_state(FeedbackStates.waiting_text)
        m4 = FakeMessage("second attempt")
        await handle_feedback_text(m4, session, state, lang="ru")
        assert m4.answers and m4.answers[0] == i18n.t("feedback_rate_limited", "ru")
        cnt = (await session.execute(text(
            "SELECT count(*) FROM feedback WHERE telegram_user_id=:u"), {"u": TEST_UID})).scalar()
        assert cnt == 1

        # 5) A menu button while waiting = cancel + that button's own action.
        await session.execute(text("DELETE FROM feedback WHERE telegram_user_id=:u"), {"u": TEST_UID})
        await state.set_state(FeedbackStates.waiting_text)
        m5 = FakeMessage(i18n.t("btn_search", "ru"))
        await handle_feedback_text(m5, session, state, lang="ru")
        assert m5.answers and m5.answers[0] == i18n.t("search_prompt", "ru")
        assert await state.get_state() is None

        # 6) An overly long message is rejected; nothing is written.
        await state.set_state(FeedbackStates.waiting_text)
        m6 = FakeMessage("x" * 2001)
        await handle_feedback_text(m6, session, state, lang="en")
        assert m6.answers and m6.answers[0] == i18n.t("feedback_too_long", "en")

        # 7) The previous message is older than one hour → the limit no longer applies.
        await session.execute(text(
            "INSERT INTO feedback (telegram_user_id, text, language, created_at) "
            "VALUES (:u, 'old', 'ru', now() - interval '61 minutes')"), {"u": TEST_UID})
        m7 = FakeMessage(i18n.t("btn_feedback", "ru"))
        await handle_feedback_button(m7, session, FakeState(), lang="en")
        assert m7.answers and m7.answers[0] == i18n.t("feedback_prompt", "en")

        await session.rollback()

    # Nothing was left behind.
    async with session_factory() as s2:
        cnt = (await s2.execute(text(
            "SELECT count(*) FROM feedback WHERE telegram_user_id=:u"), {"u": TEST_UID})).scalar()
    assert cnt == 0
