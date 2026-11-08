"""Keep the OS credential store out of automated tests, including HTTP tests."""
import pytest


@pytest.fixture(autouse=True)
def isolated_keychain(monkeypatch):
    from azusa_bot.credential_store import CredentialStore
    values = {}
    class Backend:
        def get_password(self, service, account):
            return values.get((service,account))
        def set_password(self, service, account, password):
            values[(service,account)] = password
        def delete_password(self, service, account):
            values.pop((service,account),None)
    monkeypatch.setattr(CredentialStore,'backend',lambda self:Backend())
    yield values
