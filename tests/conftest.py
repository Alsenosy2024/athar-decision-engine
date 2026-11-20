import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.ingestion.loader import load_demo
from app.main import create_app


TEST_ADMIN = 'qa-admin'
TEST_PASSWORD = 'athar-test-password'


@pytest.fixture(autouse=True, scope='session')
def test_admin_env():
    with pytest.MonkeyPatch.context() as env:
        env.setenv('ATHAR_ADMIN_USERNAME', TEST_ADMIN)
        env.setenv('ATHAR_ADMIN_PASSWORD', TEST_PASSWORD)
        env.delenv('ATHAR_SECURE_COOKIE', raising=False)
        yield


def sign_in(client, username=TEST_ADMIN, password=TEST_PASSWORD):
    response = client.post('/api/auth/login', json={'username': username, 'password': password},
                           headers={'X-Athar-Login': '1'})
    assert response.status_code == 200, response.text
    client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return response.json()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / 'test.sqlite'


@pytest.fixture
def client(db_path):
    with TestClient(create_app(Settings(db_path=db_path, forecast_engine='stat', llm_provider='rules'))) as test_client:
        sign_in(test_client)
        yield test_client


@pytest.fixture(params=['contracting', 'production'])
def demo_dataset(request):
    return request.param, load_demo(request.param)
