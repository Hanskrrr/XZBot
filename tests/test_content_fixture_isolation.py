"""Shared fixtures must not read the user's editable production background."""
from pathlib import Path

from azusa_bot import lore
from support_content import seed_content


def test_seed_is_independent_of_production_persona_and_full_backgrounds(tmp_path, monkeypatch):
    production = Path(__file__).resolve().parents[1] / "content"
    original_text, original_bytes = Path.read_text, Path.read_bytes

    def read_text(path, *args, **kwargs):
        assert not path.resolve().is_relative_to(production), "fixture read production content"
        return original_text(path, *args, **kwargs)

    def read_bytes(path, *args, **kwargs):
        assert not path.resolve().is_relative_to(production), "fixture read production content"
        return original_bytes(path, *args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(Path, "read_text", read_text)
        guard.setattr(Path, "read_bytes", read_bytes)
        content = seed_content(tmp_path / "instance")
    assert not (content / "knowledge").exists()
    assert "合成测试角色" in (content / "persona.md").read_text()
    hits = lore.for_prompt(tmp_path / "instance", "合成星球")
    assert len(hits) == 1
    assert hits[0]["category"] == "fiction"
    assert "虚构星球" in hits[0]["text"]
    assert hits[0].get("always_include") is not True
    assert lore.for_prompt(tmp_path / "instance", "unrelated-test-query") == []
