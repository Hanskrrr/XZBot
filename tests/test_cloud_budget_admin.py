import time
import pytest
from fastapi.testclient import TestClient
from support_content import seed_content
from azusa_bot.app import create_app
from azusa_bot.settings import Settings, read_settings, write_settings, token


def test_account_budget_read_save_auth_and_utc_reset(tmp_path, monkeypatch):
    seed_content(tmp_path)
    write_settings(tmp_path, Settings(daily_cloud_requests=200))
    app = create_app(tmp_path)
    client = TestClient(app)
    headers = {'Authorization': 'Bearer ' + token(tmp_path, 'admin')}
    ledger = app.state.engine.ledger
    now = 1794095999  # one second before a UTC day boundary
    monkeypatch.setattr('azusa_bot.core.time.time', lambda: now)
    day = time.strftime('%Y-%m-%d', time.gmtime(now))
    with ledger.db() as db:
        db.execute('INSERT INTO budget VALUES (?,200)', (day,))
        db.execute('INSERT INTO budget VALUES (?,777)', ('topic:' + day,))
    assert client.get('/admin/cloud-budget').status_code == 401
    assert client.put('/admin/cloud-budget', json={'daily_cloud_requests': 999}).status_code == 401
    result = client.get('/admin/cloud-budget', headers=headers).json()
    assert result['used'] == 200 and result['remaining'] == 0
    assert result['resets_at'] == (now//86400+1)*86400
    original = read_settings(tmp_path).model_dump()
    for invalid in [0, 10001, 10.5, True, '100', None]:
        assert client.put('/admin/cloud-budget', headers=headers, json={'daily_cloud_requests': invalid}).status_code == 400
    result = client.put('/admin/cloud-budget', headers=headers, json={'daily_cloud_requests': 400}).json()
    assert result['used'] == 200 and result['remaining'] == 200
    assert read_settings(tmp_path).model_dump() == original | {'daily_cloud_requests': 400}
    now = result['resets_at']
    result = client.get('/admin/cloud-budget', headers=headers).json()
    assert result['used'] == 0 and result['remaining'] == 400
    app.state.engine.close(); app.state.subscriptions.close()
