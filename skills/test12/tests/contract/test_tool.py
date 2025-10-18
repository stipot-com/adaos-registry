from adaos.sdk.skills.testing import skill


def test_happy_path():
    sut = skill()
    get_weather = sut.import_("handlers.main", "get_weather")
    res = get_weather(city="Test City")
    assert isinstance(res, dict) and res.get("ok") is True
