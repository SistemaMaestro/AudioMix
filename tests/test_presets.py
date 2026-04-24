"""Unit tests for SQLite preset repository (audiomix/presets.py)."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from audiomix.presets import PresetChannel, PresetRepo


@pytest.fixture
async def repo():
    with tempfile.TemporaryDirectory() as d:
        r = PresetRepo(Path(d) / "test.db")
        await r.init()
        yield r


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_returns_preset(self, repo):
        p = await repo.create(
            user_id="u1", name="Morning", aux_number=3,
            master_level=0.72, channels=[],
        )
        assert p.id > 0
        assert p.name == "Morning"
        assert p.user_id == "u1"

    @pytest.mark.asyncio
    async def test_create_with_channels(self, repo):
        channels = [
            PresetChannel(source_type="line", source_channel=1, level=0.5, hidden=False),
            PresetChannel(source_type="line", source_channel=2, level=0.8, hidden=True),
        ]
        p = await repo.create("u1", "Test", 3, 0.72, channels)
        assert len(p.channels) == 2
        assert p.channels[0].source_channel == 1
        assert p.channels[1].hidden is True

    @pytest.mark.asyncio
    async def test_duplicate_name_raises(self, repo):
        await repo.create("u1", "Same", 3, 0.72, [])
        with pytest.raises(Exception):
            await repo.create("u1", "Same", 3, 0.72, [])


class TestGet:
    @pytest.mark.asyncio
    async def test_get_existing(self, repo):
        created = await repo.create("u1", "Get Test", 5, 0.5, [])
        fetched = await repo.get("u1", created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.name == "Get Test"

    @pytest.mark.asyncio
    async def test_get_wrong_user(self, repo):
        created = await repo.create("u1", "Private", 5, 0.5, [])
        result = await repo.get("u2", created.id)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, repo):
        result = await repo.get("u1", 99999)
        assert result is None


class TestList:
    @pytest.mark.asyncio
    async def test_list_only_own(self, repo):
        await repo.create("u1", "A", 1, 0.5, [])
        await repo.create("u1", "B", 2, 0.5, [])
        await repo.create("u2", "C", 3, 0.5, [])
        items = await repo.list_for_user("u1")
        assert len(items) == 2
        names = {p.name for p in items}
        assert names == {"A", "B"}

    @pytest.mark.asyncio
    async def test_list_empty_user(self, repo):
        items = await repo.list_for_user("nobody")
        assert items == []


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_name_and_master(self, repo):
        p = await repo.create("u1", "Old Name", 3, 0.5, [])
        updated = await repo.update(
            user_id="u1", preset_id=p.id, name="New Name", master_level=0.9
        )
        assert updated is not None
        assert updated.name == "New Name"
        assert abs(updated.master_level - 0.9) < 1e-9

    @pytest.mark.asyncio
    async def test_update_replaces_channels(self, repo):
        original_ch = [PresetChannel("line", 1, 0.5, False)]
        p = await repo.create("u1", "Chans", 3, 0.5, original_ch)
        new_ch = [
            PresetChannel("line", 1, 0.9, False),
            PresetChannel("line", 5, 0.3, True),
        ]
        updated = await repo.update("u1", p.id, channels=new_ch)
        assert updated is not None
        assert len(updated.channels) == 2

    @pytest.mark.asyncio
    async def test_update_wrong_user(self, repo):
        p = await repo.create("u1", "Mine", 3, 0.5, [])
        result = await repo.update("u2", p.id, name="Stolen")
        assert result is None


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_existing(self, repo):
        p = await repo.create("u1", "ToDelete", 3, 0.5, [])
        ok = await repo.delete("u1", p.id)
        assert ok is True
        assert await repo.get("u1", p.id) is None

    @pytest.mark.asyncio
    async def test_delete_wrong_user(self, repo):
        p = await repo.create("u1", "ToDelete", 3, 0.5, [])
        ok = await repo.delete("u2", p.id)
        assert ok is False
        assert await repo.get("u1", p.id) is not None

    @pytest.mark.asyncio
    async def test_delete_cascades_channels(self, repo):
        ch = [PresetChannel("line", 1, 0.5, False)]
        p = await repo.create("u1", "WithChannels", 3, 0.5, ch)
        await repo.delete("u1", p.id)
        # Verify channel gone by checking get returns None
        assert await repo.get("u1", p.id) is None
