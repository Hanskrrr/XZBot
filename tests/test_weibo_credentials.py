"""Complete Cookie stays write-only; malformed headers never reach a source."""
import json

import pytest

from azusa_bot.subscription_credentials import credential_status, credential_values, update_credentials


def test_cookie_normalized_private_and_legacy_retained(tmp_path):
    value = "SUB=synthetic-session;  SUBP=synthetic-extra; _T_WM=123; XSRF-TOKEN=abc%2B123"
    update_credentials(tmp_path, {"weibo_sub": "legacy-synthetic"})
    result = update_credentials(tmp_path, {"weibo_cookie": value})
    assert result["fields"]["weibo_cookie"] == {"configured": True}
    assert "synthetic" not in json.dumps(result)
    saved = credential_values(tmp_path, ("weibo_cookie", "weibo_sub"))
    assert saved["weibo_cookie"] == value.replace(";  ", "; ")
    assert saved["weibo_sub"] == "legacy-synthetic"
    update_credentials(tmp_path, {"weibo_cookie": ""})
    assert credential_status(tmp_path)["fields"]["weibo_cookie"]["configured"]
    update_credentials(tmp_path, {"clear": ["weibo_cookie"]})
    assert not credential_status(tmp_path)["fields"]["weibo_cookie"]["configured"]
    assert credential_status(tmp_path)["fields"]["weibo_sub"]["configured"]


@pytest.mark.parametrize("value", [
    "SUB=x\r\nAuthorization: bad", "SUB=x\x00", "Cookie: SUB=x", "Set-Cookie: SUB=x",
    "SUB=x; Domain=.weibo.cn", "SUB=x; Path=/", "SUB=x; Secure", "SUB=x; HttpOnly",
    "SUB=x; SUB=y", "SUB=", "SUBP=x", "SUB=x;", 'SUB="x"', "SUB=x\\y", "SUB=x y",
    "SUB=中文", "SUB=" + "x" * 8192,
])
def test_bad_headers_rejected_without_changing_existing(tmp_path, value):
    update_credentials(tmp_path, {"weibo_cookie": "SUB=original"})
    with pytest.raises(ValueError, match="^subscription_credentials_value_invalid$"):
        update_credentials(tmp_path, {"weibo_cookie": value, "steam_api_key": "not-written"})
    assert credential_values(tmp_path, ("weibo_cookie", "steam_api_key")) == {
        "weibo_cookie": "SUB=original", "steam_api_key": ""}


def test_single_value_fields_still_reject_cookie_headers(tmp_path):
    with pytest.raises(ValueError):
        update_credentials(tmp_path, {"weibo_sub": "SUB=one; SUBP=two"})
    with pytest.raises(ValueError):
        update_credentials(tmp_path, {"weibo_cookie": "SUB=new", "clear": ["weibo_cookie"]})
