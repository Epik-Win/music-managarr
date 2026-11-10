"""cycle_schedule: the persisted multi-user cycle timer."""
from datetime import timedelta

import cycle_schedule


def test_first_pass_deferred_and_kept_across_restarts(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    first = cycle_schedule.next_due(cycle_schedule.defer_first_pass(3600))
    assert first is not None
    # A "restart" reads the same due time back instead of pushing it out.
    assert cycle_schedule.next_due() == first
    assert not cycle_schedule.pass_in_progress(cycle_schedule.load())


def test_pass_lifecycle_tracks_users_and_next_due(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    cycle_schedule.begin_pass()
    data = cycle_schedule.load()
    assert cycle_schedule.pass_in_progress(data)
    assert cycle_schedule.users_done(data) == []

    cycle_schedule.mark_user_done('alice')
    cycle_schedule.mark_user_done('alice')
    cycle_schedule.mark_user_done('bob')
    assert cycle_schedule.users_done(cycle_schedule.load()) == ['alice', 'bob']

    before = cycle_schedule.now()
    data = cycle_schedule.finish_pass(604800)
    assert not cycle_schedule.pass_in_progress(data)
    assert cycle_schedule.users_done(data) == []
    assert cycle_schedule.next_due(data) - before >= timedelta(seconds=604800 - 5)


def test_interrupted_pass_is_detected(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    cycle_schedule.finish_pass(60)
    cycle_schedule.begin_pass()
    cycle_schedule.mark_user_done('alice')
    # "Restart": nothing on disk says the pass completed after it started.
    data = cycle_schedule.load()
    assert cycle_schedule.pass_in_progress(data)
    assert cycle_schedule.users_done(data) == ['alice']


def test_missing_or_corrupt_schedule_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    assert cycle_schedule.load() == {}
    (tmp_path / 'cycle_schedule.json').write_text('not json')
    assert cycle_schedule.load() == {}
    assert cycle_schedule.next_due() is None
