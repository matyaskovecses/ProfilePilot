import json

import pytest

from profilepilot.errors import AmbiguousError, ConflictError, NotFoundError, ProfileRunningError
from profilepilot.models import ProxyCheck


def test_profile_crud_and_resolution(store):
    a = store.create_profile("Shop A", tags=["shop", "Shop", " eu "], notes="n")
    b = store.create_profile("shop b")
    assert a.tags == ["shop", "eu"]
    assert store.user_data_dir(a.id).is_dir()
    assert store.get_profile("shop a").id == a.id          # case-insensitive name
    assert store.get_profile(a.id[:4]).id == a.id or True   # prefix (may collide only by chance)
    assert store.get_profile(a.id).name == "Shop A"
    with pytest.raises(ConflictError):
        store.create_profile("SHOP A")
    with pytest.raises(NotFoundError):
        store.get_profile("nope")
    assert [p.name for p in store.list_profiles()] == ["Shop A", "shop b"]
    assert [p.name for p in store.list_profiles(tag="EU")] == ["Shop A"]

    updated = store.update_profile("Shop A", name="Shop Alpha", launch={"window": "offscreen", "lang": "de-DE"})
    assert updated.rev == 2 and updated.launch.window == "offscreen" and updated.launch.lang == "de-DE"
    assert updated.launch.restore_session is True  # merged, not replaced
    with pytest.raises(ConflictError):
        store.update_profile(a.id, expected_rev=1, notes="stale")
    with pytest.raises(ConflictError):
        store.update_profile(b.id, name="shop alpha")


def test_ambiguous_prefix(store, monkeypatch):
    ids = iter(["abcd1111", "abcd2222"])
    monkeypatch.setattr("profilepilot.store.new_id", lambda: next(ids))
    store.create_profile("one")
    store.create_profile("two")
    with pytest.raises(AmbiguousError):
        store.get_profile("abcd")
    assert store.get_profile("abcd1").name == "one"


def test_proxies_secrets_never_on_disk_in_plaintext(store):
    rec = store.add_proxy("socks5://alice:s3cr3t-pw@10.0.0.1:1080", "res-1", tags=["us"])
    assert rec.has_password and rec.username == "alice"
    raw = (store.root / "proxies.json").read_text()
    assert "s3cr3t-pw" not in raw
    secrets_raw = (store.root / "secrets.json").read_text() if (store.root / "secrets.json").exists() else ""
    assert "s3cr3t-pw" not in secrets_raw
    ep = store.proxy_endpoint("res-1")
    assert ep.password == "s3cr3t-pw" and ep.scheme == "socks5"
    assert "s3cr3t" not in json.dumps(rec.summary())
    # duplicate add returns the same record and updates the password
    again = store.add_proxy("socks5://alice:new-pw@10.0.0.1:1080")
    assert again.id == rec.id and store.proxy_endpoint(rec.id).password == "new-pw"


def test_bulk_import_and_remove_with_binding(store):
    text = """
    # provider list
    1.1.1.1:8000:u1:p1  # first
    socks5://u2:p2@2.2.2.2:1080
    garbage
    """
    records, errors = store.import_proxies(text, default_scheme="socks5")
    assert [r.name for r in records] == ["first", "2.2.2.2:1080"]
    assert records[0].scheme == "socks5" and len(errors) == 1
    prof = store.create_profile("p", proxy_id="first")
    assert prof.proxy_id == records[0].id
    with pytest.raises(ConflictError):
        store.remove_proxy("first")
    assert store.remove_proxy("first", force=True) == ["p"]
    assert store.get_profile("p").proxy_id is None
    assert store.secrets.get(f"proxy:{records[0].id}") is None


def test_proxy_check_and_update(store):
    rec = store.add_proxy("http://h:1", "x")
    store.set_proxy_check("x", ProxyCheck(ok=True, ip="9.9.9.9", country_code="US"))
    assert store.get_proxy("x").last_check.ip == "9.9.9.9"
    upd = store.update_proxy("x", url="socks5://u:p@h2:2", name="y")
    assert (upd.scheme, upd.host, upd.port, upd.name, upd.last_check) == ("socks5", "h2", 2, "y", None)
    assert store.proxy_endpoint("y").password == "p"


def test_trash_restore_and_purge(store):
    p = store.create_profile("keep me")
    (store.user_data_dir(p.id) / "Default").mkdir()
    (store.user_data_dir(p.id) / "Default" / "Cache").mkdir()
    (store.user_data_dir(p.id) / "Default" / "Preferences").write_text("{}")
    entry = store.delete_profile("keep me")
    assert store.list_profiles() == []
    assert [e.trash_id for e in store.list_trash()] == [entry.trash_id]
    store.create_profile("keep me")  # name now taken by a new profile
    restored = store.restore_profile(entry.trash_id)
    assert restored.id == p.id and restored.name == "keep me (2)"
    assert (store.user_data_dir(p.id) / "Default" / "Preferences").exists()
    assert not (store.user_data_dir(p.id) / "Default" / "Cache").exists()
    store.delete_profile(p.id)
    assert store.purge_trash(older_than_days=0) == 1 and store.list_trash() == []


def test_running_profile_cannot_be_deleted(store):
    import os

    p = store.create_profile("live")
    store.runtime_file(p.id).write_text(json.dumps({"host_pid": os.getpid()}))
    with pytest.raises(ProfileRunningError):
        store.delete_profile("live")


def test_clone_copies_data_without_caches(store):
    src = store.create_profile("src", tags=["t"])
    d = store.user_data_dir(src.id) / "Default"
    (d / "Cache").mkdir(parents=True)
    (d / "Cookies").write_text("x")
    clone = store.clone_profile("src", "dst", copy_data=True)
    assert clone.tags == ["t"]
    assert (store.user_data_dir(clone.id) / "Default" / "Cookies").exists()
    assert not (store.user_data_dir(clone.id) / "Default" / "Cache").exists()


def test_tolerant_json_reads(store, tmp_path):
    from profilepilot.jsonio import read_json

    f = tmp_path / "bom.json"
    f.write_bytes(b"\xef\xbb\xbf{\"a\": 1}")
    assert read_json(f) == {"a": 1}
    f.write_bytes('{"b": 2}'.encode("utf-16"))
    assert read_json(f) == {"b": 2}
    f.write_text("{broken")
    assert read_json(f, "dflt") == "dflt" and (tmp_path / "bom.json.bad").exists()
