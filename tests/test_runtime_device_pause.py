from watchit_agents.runtime import effective_pause_until


class FakeDB:
    def __init__(self, hh, dev):
        self._hh, self._dev = hh, dev
    def get_paused_until(self, household_id):
        return self._hh
    def get_device_paused_until(self, device_id):
        return self._dev


def test_returns_none_when_neither_paused():
    assert effective_pause_until(FakeDB(None, None), "hh_1", "dev_1") is None


def test_device_pause_applies_without_household_pause():
    assert effective_pause_until(FakeDB(None, 5000), "hh_1", "dev_1") == 5000


def test_takes_the_later_of_the_two():
    assert effective_pause_until(FakeDB(9000, 5000), "hh_1", "dev_1") == 9000


def test_missing_device_id_uses_household_only():
    assert effective_pause_until(FakeDB(7000, None), "hh_1", None) == 7000
