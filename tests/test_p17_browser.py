import pytest

from qa.phase17_browser import run


@pytest.mark.parametrize('width', [1440, 390])
def test_company_onboarding_browser(width):
    run(width)
