import os
from pathlib import Path

import pytest

from qa.phase16_browser import run


@pytest.mark.parametrize('width', [1440, 390])
def test_enterprise_browser_workflow(width):
    screens = os.environ.get('ATHAR_QA_SCREENS')
    run(width, Path(screens) if screens else None)
