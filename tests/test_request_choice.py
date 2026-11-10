import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

import request_choice as rc
from downloader import Downloader


@pytest.fixture
def asking_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("REQUEST_CHOICE_TIMEOUT_SECONDS", "5")
    monkeypatch.setattr(rc, "POLL_SECONDS", 0.05)
    return tmp_path


def _bot_answers(choice, delay=0.2):
    """Play the bot: post the first new question, then press a button."""
    def run():
        deadline = time.time() + 5
        while time.time() < deadline:
            new = [q for q in rc.pending() if q["status"] == "new"]
            if new:
                rc.posted(new[0]["id"], 1, 2)
                time.sleep(delay)
                rc.answer(new[0]["id"], choice, "alice")
                return
            time.sleep(0.02)
    thread = threading.Thread(target=run)
    thread.start()
    return thread


def _downloader(ai_reply):
    d = Downloader.__new__(Downloader)
    d._ask_local_ai = lambda system, prompt: ai_reply
    return d


CANDIDATES = [{"artist": "Ylvis", "title": "The Fox (Acapella)", "source": "deezer", "duration": 200},
              {"artist": "Ylvis", "title": "The Fox", "source": "deezer", "duration": 213}]


class TestAsking:
    def test_only_inside_a_request_and_with_a_bot(self, asking_env, monkeypatch):
        assert not rc.can_ask()                                   # a cycle's own pick: nobody to ask
        with rc.asking("alice"):
            assert rc.can_ask()
            monkeypatch.setenv("DISCORD_BOT_TOKEN", "")
            assert not rc.can_ask()
        with rc.asking(None):
            assert not rc.can_ask()

    def test_the_answer_comes_back(self, asking_env):
        with rc.asking("alice"):
            bot = _bot_answers(1)
            assert rc.ask("Ylvis - The Fox", "Deezer", [{"label": "a"}, {"label": "b"}], "SoundCloud") == 1
            bot.join()
        assert rc.pending() == []                                 # the question file is gone

    def test_no_answer_expires_for_the_bot_to_close(self, asking_env, monkeypatch):
        monkeypatch.setenv("REQUEST_CHOICE_TIMEOUT_SECONDS", "1")
        with rc.asking("alice"):
            assert rc.ask("Ylvis - The Fox", "Deezer", [{"label": "a"}, {"label": "b"}], "SoundCloud", suggestion=0) is None
        [question] = rc.pending()
        assert question["status"] == "expired" and question["suggestion"] == 0
        assert rc.answer(question["id"], 1, "alice") is None      # too late to press a button

    def test_an_unreachable_requester_is_not_waited_for(self, asking_env):
        def bot():
            while not rc.pending():
                time.sleep(0.02)
            rc.unreachable(rc.pending()[0]["id"])
        threading.Thread(target=bot).start()
        started = time.time()
        with rc.asking("ghost"):
            assert rc.ask("X - Y", "Deezer", [{"label": "a"}], None) is None
        assert time.time() - started < 2


class TestChoosing:
    def test_the_ai_decides_unless_it_is_only_guessing(self, asking_env, monkeypatch):
        monkeypatch.setattr(rc, "ask", lambda *a, **k: pytest.fail("asked although the AI was not guessing"))
        for confidence in (1.0, 0.5, 0.15):                       # unsure is not enough to ask
            d = _downloader({"selected_index": 2, "confidence": confidence})
            with rc.asking("alice"):
                assert d._choose_or_ask("Ylvis", "The Fox", CANDIDATES, "", "Deezer") is CANDIDATES[1]
        d = _downloader({"selected_index": 2})                    # no confidence given: its pick stands
        with rc.asking("alice"):
            assert d._choose_or_ask("Ylvis", "The Fox", CANDIDATES, "", "Deezer") is CANDIDATES[1]

    def test_the_requester_settles_a_guess(self, asking_env):
        d = _downloader({"selected_index": 1, "confidence": 0.05})
        with rc.asking("alice"):
            bot = _bot_answers(1)
            assert d._choose_or_ask("Ylvis", "The Fox", CANDIDATES, "", "Deezer") is CANDIDATES[1]
            bot.join()

    def test_no_pick_at_all_asks(self, asking_env):
        d = _downloader({})
        with rc.asking("alice"):
            bot = _bot_answers(0)
            assert d._choose_or_ask("Ylvis", "The Fox", CANDIDATES, "", "Deezer") is CANDIDATES[0]
            bot.join()

    def test_none_of_them_match_moves_on(self, asking_env):
        d = _downloader({"selected_index": 1, "confidence": 0.0})
        with rc.asking("alice"):
            bot = _bot_answers(rc.NEXT)
            assert d._choose_or_ask("Ylvis", "The Fox", CANDIDATES, "", "Deezer") is None
            bot.join()

    def test_without_an_answer_the_ais_guess_stands(self, asking_env, monkeypatch):
        monkeypatch.setattr(rc, "ask", lambda *a, **k: None)
        d = _downloader({"selected_index": 1, "confidence": 0.0})
        with rc.asking("alice"):
            assert d._choose_or_ask("Ylvis", "The Fox", CANDIDATES, "", "Deezer") is CANDIDATES[0]

    def test_the_list_names_each_candidate(self, asking_env, monkeypatch):
        asked = {}
        monkeypatch.setattr(rc, "ask", lambda request, source, options, next_step, suggestion=None:
                            asked.update(request=request, options=options, next_step=next_step,
                                         suggestion=suggestion))
        d = _downloader({"selected_index": 2, "confidence": 0.02})
        with rc.asking("alice"):
            d._choose_or_ask("Ylvis", "The Fox", CANDIDATES, "", "Deezer")
        assert asked == {"request": "Ylvis - The Fox", "next_step": "SoundCloud", "suggestion": 1,
                         "options": [{"label": "Ylvis - The Fox (Acapella)", "detail": "3:20"},
                                     {"label": "Ylvis - The Fox", "detail": "3:33"}]}


class TestDeemixTimeout:
    def test_waiting_for_the_requester_is_not_a_hung_deezer(self, asking_env, tmp_path):
        d = Downloader.__new__(Downloader)
        d._DEEMIX_TRACK_TIMEOUT = 0.3

        def deemix_that_asks(*a):
            requester = rc.current()                              # the worker thread sees the requester
            requester.waiting.set()
            time.sleep(1.0)                                       # the requester takes a while
            requester.waiting.clear()
            return True, "/music/albums/A/B/c.mp3", False

        d._download_with_deemix = deemix_that_asks
        with rc.asking("alice"):
            assert d._download_with_deemix_timeout("q", str(tmp_path), None, "A", "c") == \
                (True, "/music/albums/A/B/c.mp3", False)


class TestBotList:
    def _interaction(self, custom_id, name="alice", uid=7, values=None):
        edits, sent, deferred = [], [], []

        class Response:
            async def edit_message(self, **kw):
                edits.append(kw)

            async def send_message(self, text, ephemeral=False):
                sent.append(text)

            async def defer(self):
                deferred.append(True)

        class Message:
            deleted = False

            async def delete(self):
                Message.deleted = True

        data = {"custom_id": custom_id}
        if values is not None:
            data["values"] = values
        interaction = SimpleNamespace(type=__import__("discord").InteractionType.component, data=data,
                                      user=SimpleNamespace(name=name, id=uid, mention=f"@{name}"), guild=None,
                                      response=Response(), message=Message(),
                                      followup=SimpleNamespace(send=lambda *a, **k: asyncio.sleep(0)))
        return interaction, edits, sent, deferred

    def _asked(self):
        question = {"id": "q1", "status": "new", "discord_user": "alice", "request": "Ylvis - The Fox",
                    "source": "Deezer", "next_step": "SoundCloud", "suggestion": 0,
                    "options": [{"label": "Ylvis - The Fox (Acapella)", "detail": "3:20"},
                                {"label": "Ylvis - The Fox", "detail": "The Fox · 3:33"}],
                    "created": time.time(), "timeout": 900}
        rc._write(question)
        rc.posted("q1", 1, 2)
        return question

    def test_a_list_with_none_of_them_match_and_a_select_button(self, asking_env):
        import discord_bot
        question = self._asked()

        async def build():                                        # a View needs the bot's event loop
            return discord_bot._choice_view(question)
        view = asyncio.run(build())
        menu, button = view.children
        assert menu.custom_id == "mai-choice:q1:menu" and button.custom_id == "mai-choice:q1:submit"
        assert button.label == "Select"
        assert [(o.label, o.value) for o in menu.options] == [
            ("1. Ylvis - The Fox (Acapella)", "0"), ("2. Ylvis - The Fox", "1"), ("None of them match", "next")]
        assert menu.options[0].description == "3:20 · my best guess"
        assert menu.options[2].description == "Try SoundCloud instead"

    def test_picking_then_select_sends_the_choice_back(self, asking_env):
        import discord_bot
        self._asked()
        pick, _, _, deferred = self._interaction("mai-choice:q1:menu", values=["1"])
        asyncio.run(discord_bot._on_choice_button(pick))
        assert deferred and rc.load("q1")["status"] == "asked" and rc.load("q1")["selected"] == 1
        submit, edits, sent, _ = self._interaction("mai-choice:q1:submit")
        asyncio.run(discord_bot._on_choice_button(submit))
        assert rc.load("q1")["status"] == "answered" and rc.load("q1")["answer"] == 1
        assert submit.message.deleted and edits == []                 # nothing of the question stays
        assert sent == ["✅ Got it: downloading **Ylvis - The Fox**."]  # seen only by whoever pressed Select

    def test_none_of_them_match_continues_the_pipeline(self, asking_env):
        import discord_bot
        self._asked()
        asyncio.run(discord_bot._on_choice_button(self._interaction("mai-choice:q1:menu", values=["next"])[0]))
        submit, _, sent, _ = self._interaction("mai-choice:q1:submit")
        asyncio.run(discord_bot._on_choice_button(submit))
        assert rc.load("q1")["answer"] == rc.NEXT and "trying SoundCloud" in sent[0] and submit.message.deleted

    def test_select_without_a_pick_asks_for_one(self, asking_env):
        import discord_bot
        self._asked()
        submit, edits, sent, _ = self._interaction("mai-choice:q1:submit")
        asyncio.run(discord_bot._on_choice_button(submit))
        assert rc.load("q1")["status"] == "asked" and edits == [] and "Pick an option" in sent[0]

    def test_someone_else_cannot_choose(self, asking_env):
        import discord_bot
        self._asked()
        pick, edits, sent, _ = self._interaction("mai-choice:q1:menu", name="bob", uid=8, values=["0"])
        asyncio.run(discord_bot._on_choice_button(pick))
        assert "selected" not in rc.load("q1") and edits == [] and sent

    def test_an_expired_question_is_deleted_from_the_thread(self, asking_env, monkeypatch):
        import discord_bot
        self._asked()
        question = rc.load("q1")
        question["status"] = "expired"
        rc._write(question)
        deleted = []

        class Thread:
            async def fetch_message(self, message_id):
                return SimpleNamespace(delete=lambda: asyncio.sleep(0, deleted.append(message_id)))

        monkeypatch.setattr(discord_bot, "_requests_channel", lambda: asyncio.sleep(0, None))
        monkeypatch.setattr(discord_bot, "bot", SimpleNamespace(get_channel=lambda thread_id: Thread()))
        asyncio.run(discord_bot._deliver_request_choices())
        assert deleted == [2] and rc.load("q1") is None


class TestNotes:
    def test_what_each_question_settled_is_kept_for_the_final_message(self, asking_env, monkeypatch):
        with rc.asking("alice") as requester:
            bot = _bot_answers(1)
            rc.ask("Ylvis - The Fox", "Deezer", [{"label": "Ylvis - The Fox (Acapella)"},
                                                 {"label": "Ylvis - The Fox"}], "SoundCloud")
            bot.join()
            bot = _bot_answers(rc.NEXT)
            rc.ask("Ylvis - The Fox", "SoundCloud", [{"label": "a"}], "YouTube")
            bot.join()
            monkeypatch.setenv("REQUEST_CHOICE_TIMEOUT_SECONDS", "1")
            rc.ask("Ylvis - The Fox", "YouTube", [{"label": "b"}], None)
        assert requester.notes == ["Deezer: you picked Ylvis - The Fox",
                                   "SoundCloud: none of its results matched",
                                   "YouTube: no answer in time, my best guess was taken"]


def test_a_question_whose_asker_is_gone_is_closed_after_its_deadline(asking_env):
    # A cycle that restarts while questions are open: nothing else would
    # ever answer or expire them.
    rc._write({"id": "old", "status": "asked", "discord_user": "alice", "request": "X - Y", "source": "Deezer",
               "options": [{"label": "a"}], "created": time.time() - 2000, "timeout": 900, "message_id": 5,
               "thread_id": 1})
    rc._write({"id": "live", "status": "asked", "discord_user": "alice", "request": "X - Z", "source": "Deezer",
               "options": [{"label": "b"}], "created": time.time(), "timeout": 900, "message_id": 6, "thread_id": 1})
    assert [q["id"] for q in rc.pending()] == ["old"] and rc.load("old")["status"] == "expired"
    assert rc.load("live")["status"] == "asked"


def test_queued_requests_in_a_cycle_never_ask():
    import inspect
    import app
    assert ".asking(" not in inspect.getsource(app.MusicAIAnalyzer._process_discord_requests)
