"""Unit tests for Maestro token verification with respx mock (audiomix/auth.py)."""
import asyncio
import pytest
import respx
import httpx

from audiomix.config import MaestroConfig
from audiomix.auth import MaestroAuth, MaestroUser


def _cfg() -> MaestroConfig:
    return MaestroConfig(
        base_url="https://api.maestro.test/api",
        verify_token_path="/Auth/verificar-token",
        token_cache_ttl_seconds=300,
        request_timeout_seconds=5,
    )


VALID_RESPONSE = {
    "sucesso": True,
    "token": "same_token",
    "usuario": {
        "id": "42",
        "nome": "João Silva",
        "email": "joao@test.com",
    },
}

INVALID_RESPONSE = {"sucesso": False, "mensagem": "Token inválido"}


@pytest.fixture
async def auth():
    a = MaestroAuth(_cfg())
    await a.start()
    yield a
    await a.stop()


class TestVerify:
    @pytest.mark.asyncio
    @respx.mock
    async def test_valid_token(self, auth):
        respx.post("https://api.maestro.test/api/Auth/verificar-token").mock(
            return_value=httpx.Response(200, json=VALID_RESPONSE)
        )
        user = await auth.verify("valid_token_here")
        assert isinstance(user, MaestroUser)
        assert user.id == "42"
        assert user.name == "João Silva"
        assert user.email == "joao@test.com"

    @pytest.mark.asyncio
    @respx.mock
    async def test_invalid_token_401(self, auth):
        respx.post("https://api.maestro.test/api/Auth/verificar-token").mock(
            return_value=httpx.Response(401, json=INVALID_RESPONSE)
        )
        user = await auth.verify("bad_token")
        assert user is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_invalid_token_sucesso_false(self, auth):
        respx.post("https://api.maestro.test/api/Auth/verificar-token").mock(
            return_value=httpx.Response(200, json={"sucesso": False})
        )
        user = await auth.verify("bad_token_2")
        assert user is None

    @pytest.mark.asyncio
    async def test_empty_token(self, auth):
        result = await auth.verify("")
        assert result is None

    @pytest.mark.asyncio
    async def test_none_token(self, auth):
        result = await auth.verify(None)
        assert result is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_cache_hit(self, auth):
        route = respx.post("https://api.maestro.test/api/Auth/verificar-token").mock(
            return_value=httpx.Response(200, json=VALID_RESPONSE)
        )
        token = "cached_token"
        u1 = await auth.verify(token)
        u2 = await auth.verify(token)
        # Should only call Maestro once
        assert route.call_count == 1
        assert u1 == u2

    @pytest.mark.asyncio
    @respx.mock
    async def test_invalidate_clears_cache(self, auth):
        route = respx.post("https://api.maestro.test/api/Auth/verificar-token").mock(
            return_value=httpx.Response(200, json=VALID_RESPONSE)
        )
        token = "to_invalidate"
        await auth.verify(token)
        auth.invalidate(token)
        await auth.verify(token)
        # Should call Maestro twice (cache was cleared)
        assert route.call_count == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_network_error_returns_none(self, auth):
        respx.post("https://api.maestro.test/api/Auth/verificar-token").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        user = await auth.verify("any_token")
        assert user is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_server_error_returns_none(self, auth):
        respx.post("https://api.maestro.test/api/Auth/verificar-token").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        user = await auth.verify("any_token")
        assert user is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_missing_fields_returns_none(self, auth):
        respx.post("https://api.maestro.test/api/Auth/verificar-token").mock(
            return_value=httpx.Response(200, json={"sucesso": True, "usuario": {}})
        )
        user = await auth.verify("incomplete_token")
        assert user is None
