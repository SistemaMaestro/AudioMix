"""Shared pytest fixtures and asyncio mode config."""
import pytest


# Make all tests in this directory use asyncio event loop automatically.
# Requires pytest-asyncio >= 0.21 with "asyncio_mode = auto" in pyproject / pytest.ini.
