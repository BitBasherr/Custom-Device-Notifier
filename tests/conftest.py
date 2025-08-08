import pytest


@pytest.fixture(autouse=True)
def _always_enable_custom_integrations(enable_custom_integrations):
    yield
