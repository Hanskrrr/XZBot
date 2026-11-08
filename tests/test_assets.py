"""Synthetic, offline tests of reviewed expression images and profile boundaries."""
import base64
import io
import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
import pytest

from azusa_bot import assets
from azusa_bot.app import create_app, backup_project
from azusa_bot.assets import AssetError, AssetStore
from azusa_bot.settings import Settings, write_settings


def image_bytes(fmt="PNG", *, animated=False, size=(12, 10), color="red"):
    stream = io.BytesIO()
    image = Image.new("RGB", size, color)
    options = {}
    if animated:
        options = dict(save_all=True, append_images=[Image.new("RGB", size, "blue")], duration=[80, 140], loop=0)
    if fmt == "PNG":
        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", "private source metadata")
        options["pnginfo"] = info
    image.save(stream, format=fmt, **options)
    return stream.getvalue()


def payload(name="开心", *, raw=None, **fields):
    return dict(name=name, content_base64=base64.b64encode(raw or image_bytes()).decode(), **fields)


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "GIF"])
def test_upload_validated_formats_metadata_and_private_permissions(tmp_path, fmt):
    store = AssetStore(tmp_path)
    row = store.upload(payload(raw=image_bytes(fmt)))
    assert row["approved"] is True and row["auto_send"] is False
    assert row["available"] is True and row["format"] == fmt.lower()
    assert row["width"] == 12 and row["height"] == 10
    assert "file" not in row and str(tmp_path) not in json.dumps(store.list())
    raw, mime = store.preview(row["id"])
    assert mime == "image/" + fmt.lower()
    with Image.open(io.BytesIO(raw)) as image:
        assert "Comment" not in image.info
    manifest = json.loads((store.base / "manifest.json").read_text())
    path = store.base / manifest["开心"]["file"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert (store.base / "manifest.json").stat().st_mode & 0o777 == 0o600


def test_animated_gif_is_preserved_and_bounded(tmp_path, monkeypatch):
    store = AssetStore(tmp_path)
    row = store.upload(payload(raw=image_bytes("GIF", animated=True)))
    raw, mime = store.preview(row["id"])
    with Image.open(io.BytesIO(raw)) as image:
        assert image.n_frames == 2 and mime == "image/gif"
        image.seek(0); first = image.convert("RGB").getpixel((0, 0))
        image.seek(1); second = image.convert("RGB").getpixel((0, 0))
        assert first != second and image.info["duration"] == 140
    monkeypatch.setattr(assets, "MAX_FRAMES", 1)
    with pytest.raises(AssetError, match="asset_dimensions_exceeded"):
        store.upload(payload("超帧", raw=image_bytes("GIF", animated=True)))


@pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
def test_other_animated_formats_require_gif(tmp_path, fmt):
    with pytest.raises(AssetError, match="asset_animation_requires_gif"):
        AssetStore(tmp_path).upload(payload(raw=image_bytes(fmt, animated=True)))


@pytest.mark.parametrize("raw", [b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"not an image", b"GIF89a"])
def test_fake_images_and_svg_never_written(tmp_path, raw):
    store = AssetStore(tmp_path)
    with pytest.raises(AssetError, match="asset_image_invalid"):
        store.upload(payload(raw=raw))
    assert not (store.base / "manifest.json").exists()


def test_decoded_size_pixel_and_total_animation_limits(tmp_path, monkeypatch):
    store = AssetStore(tmp_path)
    with pytest.raises(AssetError, match="asset_size_exceeded"):
        store.upload(payload(raw=b"x" * (assets.MAX_BYTES + 1)))
    monkeypatch.setattr(assets, "MAX_PIXELS", 100)
    with pytest.raises(AssetError, match="asset_dimensions_exceeded"):
        store.upload(payload(raw=image_bytes(size=(11, 10))))
    monkeypatch.setattr(assets, "MAX_PIXELS", 1000)
    monkeypatch.setattr(assets, "MAX_TOTAL_PIXELS", 150)
    with pytest.raises(AssetError, match="asset_dimensions_exceeded"):
        store.upload(payload(raw=image_bytes("GIF", animated=True)))


def test_auto_opt_in_keyword_catalog_and_local_only_selection(tmp_path):
    store = AssetStore(tmp_path)
    row = store.upload(payload(keywords=["开心", "快乐"], emotion="喜悦"))
    assert store.catalog("今天好开心") == []
    explicit = store.prepare(tmp_path / "out", "开心")
    assert explicit["mode"] == "image" and explicit["text"] == ""
    with pytest.raises(AssetError, match="asset_not_approved"):
        store.prepare(tmp_path / "out", row["id"], automatic=True)
    store.update(row["id"], {"auto_send": True})
    choices = store.catalog("今天好开心")
    assert len(choices) == 1 and choices[0]["id"] == row["id"]
    assert set(choices[0]) == {"id", "name", "keywords", "emotion", "revision"}
    assert store.catalog("明天下雨") == []
    for address in ["https://example.test/image.png", "../../secret.png", "/tmp/secret.png"]:
        with pytest.raises(AssetError, match="asset_not_found"):
            store.prepare(tmp_path / "out", address)
    assert str(tmp_path) not in json.dumps(choices)


@pytest.mark.parametrize("mutation", ["auto_send", "approved", "rename", "delete", "file"])
def test_prepared_image_is_revoked_after_any_edit_delete_or_file_tampering(tmp_path, mutation):
    store = AssetStore(tmp_path)
    row = store.upload(payload(auto_send=True))
    reply = store.prepare(tmp_path / "out", row["id"], automatic=True)
    ref = reply["asset_reference"]
    assert store.allowed(ref) and assets.still_approved(tmp_path, ref)
    if mutation == "file":
        manifest = json.loads((store.base / "manifest.json").read_text())
        (store.base / manifest["开心"]["file"]).write_bytes(image_bytes(color="green"))
    elif mutation == "delete":
        store.delete(row["id"])
    else:
        store.update(row["id"], {"name": "新名字"} if mutation == "rename" else {mutation: False})
    assert not store.allowed(ref)
    if mutation in {"file", "delete", "approved", "auto_send"}:
        with pytest.raises(AssetError):
            store.prepare(tmp_path / "out", row["id"], automatic=True)


def test_profile_assets_cannot_cross_and_foreign_ref_is_invalid(tmp_path):
    first, second = AssetStore(tmp_path / "one"), AssetStore(tmp_path / "two")
    row = first.upload(payload(auto_send=True))
    reply = first.prepare(tmp_path / "out", row["id"], automatic=True)
    assert second.list()["assets"] == []
    assert not second.allowed(reply["asset_reference"])
    with pytest.raises(AssetError, match="asset_not_found"):
        second.prepare(tmp_path / "out", row["id"])


@pytest.mark.parametrize("filename", ["smile.png", "a" * 32 + ".png"])
def test_legacy_manifest_remains_usable_and_manual_blob_not_deleted(tmp_path, filename):
    store = AssetStore(tmp_path)
    store.base.mkdir(parents=True)
    raw = image_bytes(); (store.base / filename).write_bytes(raw)
    (store.base / "manifest.json").write_text(json.dumps({"笑": {"file": filename, "approved": True}}))
    row = store.list()["assets"][0]
    assert row["id"].startswith("legacy-") and row["available"]
    result = store.prepare(tmp_path / "out", "笑")
    assert Path(result["media_path"]).read_bytes() == raw
    store.update(row["id"], {"name": "微笑", "keywords": ["开心"]})
    assert not store.allowed(result["asset_reference"])
    assert store.list()["assets"][0]["id"] == row["id"]
    store.delete(row["id"])
    assert (store.base / filename).exists() and store.list()["assets"] == []


def test_asset_directory_symlink_cannot_write_or_enumerate_target(tmp_path):
    root, external = tmp_path / "root", tmp_path / "outside"
    (root / "content").mkdir(parents=True); external.mkdir()
    (root / "content/assets").symlink_to(external)
    store = AssetStore(root)
    with pytest.raises(AssetError, match="asset_directory_invalid"): store.list()
    with pytest.raises(AssetError, match="asset_directory_invalid"): store.upload(payload())
    assert list(external.iterdir()) == []


def test_bad_optional_catalog_does_not_break_chat_but_admin_can_see_error(tmp_path):
    store = AssetStore(tmp_path)
    store.base.mkdir(parents=True)
    (store.base / "manifest.json").write_text("broken legacy manifest")
    assert assets.catalog(tmp_path, "开心") == []
    with pytest.raises(AssetError, match="asset_manifest_invalid"):
        store.list()


def test_prepare_filesystem_race_falls_back_with_safe_code(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs): raise OSError("synthetic inaccessible file")
    monkeypatch.setattr(AssetStore, "prepare", unavailable)
    with pytest.raises(AssetError, match="asset_file_unavailable"):
        assets.prepare_asset(tmp_path, tmp_path / "out", "anything")


@pytest.mark.parametrize("change", ["rename", "approved", "auto_send", "delete", "file"])
def test_trace_references_drop_stale_names_after_edit_disable_delete_or_tampering(tmp_path, change):
    store = AssetStore(tmp_path)
    row = store.upload(payload(auto_send=True))
    candidate = store.catalog("开心")[0]
    reference = {key: candidate[key] for key in ("id", "revision", "name")}
    reference["name"] = "untrusted historical name /private/secret.png"
    resolved = assets.resolve_references(tmp_path, [reference])
    assert resolved == [{"id": row["id"], "revision": candidate["revision"], "available": True, "name": "开心"}]
    if change == "file":
        data = json.loads((store.base / "manifest.json").read_text())
        (store.base / data["开心"]["file"]).write_bytes(image_bytes(color="green"))
    elif change == "delete":
        store.delete(row["id"])
    elif change == "rename":
        store.update(row["id"], {"name": "新名字"})
    else:
        store.update(row["id"], {change: False})
    assert assets.resolve_references(tmp_path, [reference]) == [
        {"id": row["id"], "revision": candidate["revision"], "available": False}]


def test_trace_reference_limits_validation_profile_isolation_and_broken_manifest(tmp_path):
    first, other = tmp_path / "first", tmp_path / "other"
    store = AssetStore(first)
    store.upload(payload(auto_send=True))
    reference = store.catalog("开心")[0]
    assert len(assets.resolve_references(first, [reference] * 30)) == 20
    assert assets.resolve_references(first, [None, {}, {"id": "../../secret", "revision": "legacy"}]) == []
    assert assets.resolve_references(first, "bad") == []
    assert assets.resolve_references(other, [reference]) == [
        {"id": reference["id"], "revision": reference["revision"], "available": False}]
    (store.base / "manifest.json").write_text("broken")
    assert assets.resolve_references(first, [reference]) == [
        {"id": reference["id"], "revision": reference["revision"], "available": False}]


@pytest.mark.parametrize("path_type", ["traversal", "absolute", "symlink", "directory_symlink", "manifest_symlink"])
def test_reject_paths_and_symlinks(tmp_path, path_type):
    root = tmp_path / "root"; store = AssetStore(root)
    store.base.mkdir(parents=True)
    outside = tmp_path / "outside.png"; outside.write_bytes(image_bytes())
    file = "../../../../outside.png"
    if path_type == "absolute": file = str(outside)
    if path_type == "symlink":
        (store.base / "alias.png").symlink_to(outside); file = "alias.png"
    if path_type == "directory_symlink":
        (store.base / "alias").symlink_to(tmp_path); file = "alias/outside.png"
    manifest = store.base / "manifest.json"
    data = json.dumps({"x": {"file": file, "approved": True}})
    if path_type == "manifest_symlink":
        source = tmp_path / "outside.json"; source.write_text(data); manifest.symlink_to(source)
        with pytest.raises(AssetError, match="asset_manifest_invalid"): store.list()
    else:
        manifest.write_text(data)
        assert not store.list()["assets"][0]["available"]
        with pytest.raises(AssetError, match="asset_file_invalid"): store.prepare(tmp_path / "out", "x")
    assert outside.read_bytes() == image_bytes()


def test_upload_failure_and_library_quota_do_not_leave_partial_files(tmp_path, monkeypatch):
    store = AssetStore(tmp_path)
    def fail(*args): raise OSError("synthetic write failure")
    with monkeypatch.context() as patch:
        patch.setattr(assets, "private_write", fail)
        with pytest.raises(OSError): store.upload(payload())
    assert list(store.base.iterdir()) == []
    store.upload(payload())
    monkeypatch.setattr(assets, "MAX_FILES", 1)
    with pytest.raises(AssetError, match="asset_library_limit"): store.upload(payload("第二张"))
    assert len(store.list()["assets"]) == 1


@pytest.fixture
def admin(tmp_path):
    root = tmp_path / "bot"
    (root / "content/lore").mkdir(parents=True)
    (root / "content/persona.md").write_text("测试角色")
    write_settings(root, Settings())
    app = create_app(root)
    headers = {"Authorization": "Bearer " + (root / "data/admin.token").read_text()}
    with TestClient(app) as client:
        yield root, app, client, headers
    app.state.engine.close()


def test_authenticated_admin_crud_preview_profile_pin_and_backup(admin, tmp_path):
    root, app, client, headers = admin
    assert client.get("/admin/assets").status_code == 401
    assert client.post("/admin/assets", json=payload()).status_code == 401
    row = client.post("/admin/assets", headers=headers, json=payload()).json()["asset"]
    url = "/admin/assets/" + row["id"]
    assert client.get(url + "/preview").status_code == 401
    preview = client.get(url + "/preview", headers=headers)
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/png"
    assert preview.headers["cache-control"] == "no-store" and preview.headers["x-content-type-options"] == "nosniff"
    assert client.patch(url, headers=headers, json={"auto_send": True}).json()["asset"]["auto_send"]
    assert client.post("/admin/assets", headers=headers, content="broken").status_code == 400
    backup = backup_project(root, app.state.engine)
    assert AssetStore(backup).list()["assets"][0]["id"] == row["id"]
    other = app.state.engine.profiles.create({"name": "第二角色"})
    app.state.engine.activate(other["id"])
    assert client.get("/admin/assets", headers=headers).json()["assets"] == []
    assert client.patch(url, headers=dict(headers, **{"X-XZBot-Profile": "default"}), json={"approved": False}).status_code == 409
    assert client.get(url + "/preview", headers=headers).status_code == 404
    app.state.engine.activate("default")
    assert client.delete(url, headers=headers).json()["deleted"] is True
    assert client.get(url + "/preview", headers=headers).status_code == 404


def test_asset_http_limit_is_scoped_and_decoded_limit_still_applies(admin):
    _, _, client, headers = admin
    # A legitimate noisy RGB image exceeds the ordinary 250KB request ceiling.
    raw = Image.effect_noise((640, 640), 128)
    buffer = io.BytesIO(); raw.save(buffer, format="PNG")
    assert len(buffer.getvalue()) > 250000
    result = client.post("/admin/assets", headers=headers, json=payload(raw=buffer.getvalue()))
    assert result.status_code == 200
    too_large = client.post("/admin/assets", headers=headers, json=payload("too-big", raw=b"x"*(assets.MAX_BYTES+1)))
    assert too_large.status_code == 400 and too_large.json()["detail"] == "asset_size_exceeded"
    assert client.post("/admin/assets", headers=headers, content=b"x"*(5*1024*1024+1)).status_code == 413
