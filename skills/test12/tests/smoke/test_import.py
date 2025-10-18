from adaos.sdk.skills.testing import skill
import importlib


def test_imports():
    sut = skill()
    assert importlib.import_module(f"{sut.package}.handlers.main")
