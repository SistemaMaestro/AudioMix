"""Unit tests for AuxSession lock / TTL logic (audiomix/sessions.py)."""
import asyncio
import time
import pytest

from audiomix.config import SessionConfig
from audiomix.auth import MaestroUser
from audiomix.sessions import AuxSession, AuxOccupied, SessionManager


def _cfg(ttl: int = 15, hb: int = 5) -> SessionConfig:
    return SessionConfig(heartbeat_seconds=hb, ttl_seconds=ttl)


def _user(uid: str = "u1", name: str = "Alice") -> MaestroUser:
    return MaestroUser(id=uid, name=name)


@pytest.fixture
async def mgr():
    m = SessionManager(_cfg())
    await m.start()
    yield m
    await m.stop()


class TestClaim:
    @pytest.mark.asyncio
    async def test_claim_returns_session(self, mgr):
        sess = await mgr.claim(_user(), 3, "1.1.1.1")
        assert isinstance(sess, AuxSession)
        assert sess.aux_number == 3
        assert sess.user_id == "u1"

    @pytest.mark.asyncio
    async def test_same_user_reclaim_allowed(self, mgr):
        s1 = await mgr.claim(_user(), 3, "1.1.1.1")
        s2 = await mgr.claim(_user(), 3, "1.1.1.2")  # same user, different IP
        # Token should rotate
        assert s1.session_token != s2.session_token
        # Old token invalid, new token valid
        assert mgr.get_by_token(s1.session_token) is None
        assert mgr.get_by_token(s2.session_token) is not None

    @pytest.mark.asyncio
    async def test_different_user_claim_raises(self, mgr):
        await mgr.claim(_user("u1", "Alice"), 5, "1.1.1.1")
        with pytest.raises(AuxOccupied) as exc_info:
            await mgr.claim(_user("u2", "Bob"), 5, "2.2.2.2")
        assert exc_info.value.holder_name == "Alice"
        assert exc_info.value.holder_user_id == "u1"

    @pytest.mark.asyncio
    async def test_one_aux_per_user(self, mgr):
        """Claiming a new aux auto-releases the previous one."""
        s1 = await mgr.claim(_user(), 3, "1.1.1.1")
        await mgr.claim(_user(), 7, "1.1.1.1")  # same user, different aux
        # Aux 3 should now be free
        assert mgr.get_by_aux(3) is None
        assert mgr.get_by_token(s1.session_token) is None
        # Aux 7 should be held
        assert mgr.get_by_aux(7) is not None


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_updates_timestamp(self, mgr):
        sess = await mgr.claim(_user(), 3, "1.1.1.1")
        old_hb = sess.last_heartbeat
        await asyncio.sleep(0.05)
        updated = await mgr.heartbeat(sess.session_token)
        assert updated is not None
        assert updated.last_heartbeat > old_hb

    @pytest.mark.asyncio
    async def test_heartbeat_unknown_token(self, mgr):
        result = await mgr.heartbeat("nonexistent_token")
        assert result is None


class TestRelease:
    @pytest.mark.asyncio
    async def test_release_clears_session(self, mgr):
        sess = await mgr.claim(_user(), 4, "1.1.1.1")
        released = await mgr.release(sess.session_token)
        assert released is True
        assert mgr.get_by_aux(4) is None
        assert mgr.get_by_token(sess.session_token) is None

    @pytest.mark.asyncio
    async def test_release_unknown_token(self, mgr):
        result = await mgr.release("bad_token")
        assert result is False


class TestExpiry:
    @pytest.mark.asyncio
    async def test_session_expires_after_ttl(self):
        """Use TTL of 1s so the test stays fast."""
        cfg = SessionConfig(heartbeat_seconds=1, ttl_seconds=1)
        mgr = SessionManager(cfg)
        await mgr.start()
        try:
            sess = await mgr.claim(_user(), 2, "1.1.1.1")
            # Confirm active
            assert mgr.get_by_token(sess.session_token) is not None
            # Wait beyond TTL + sweeper cycle
            await asyncio.sleep(2.5)
            # Should be swept
            assert mgr.get_by_token(sess.session_token) is None
            assert mgr.get_by_aux(2) is None
        finally:
            await mgr.stop()

    @pytest.mark.asyncio
    async def test_expires_in_decreases(self, mgr):
        sess = await mgr.claim(_user(), 6, "1.1.1.1")
        ei1 = mgr.expires_in(sess)
        await asyncio.sleep(0.5)
        ei2 = mgr.expires_in(sess)
        assert ei1 >= ei2


class TestForceRelease:
    @pytest.mark.asyncio
    async def test_force_release(self, mgr):
        await mgr.claim(_user("u1", "Alice"), 8, "1.1.1.1")
        ok = await mgr.force_release(8)
        assert ok is True
        assert mgr.get_by_aux(8) is None

    @pytest.mark.asyncio
    async def test_force_release_empty(self, mgr):
        ok = await mgr.force_release(99)
        assert ok is False


class TestAllSessions:
    @pytest.mark.asyncio
    async def test_all_sessions(self, mgr):
        await mgr.claim(_user("u1", "Alice"), 1, "1.1.1.1")
        await mgr.claim(_user("u2", "Bob"), 2, "2.2.2.2")
        sessions = mgr.all_sessions()
        assert len(sessions) == 2
        aux_nums = {s.aux_number for s in sessions}
        assert aux_nums == {1, 2}
