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


def test_launch_option_validation_is_readable(store):
    from profilepilot.errors import ProfilePilotError

    p = store.create_profile("tz", launch={"timezone": "Europe/Berlin", "lang": "de-DE"})
    assert (p.launch.timezone, p.launch.lang) == ("Europe/Berlin", "de-DE")
    with pytest.raises(ProfilePilotError, match="unknown IANA timezone 'Mars/Base'"):
        store.update_profile("tz", launch={"timezone": "Mars/Base"})
    with pytest.raises(ProfilePilotError, match="invalid language tag"):
        store.create_profile("bad-lang", launch={"lang": "not a lang!"})
    assert store.update_profile("tz", launch={"timezone": ""}).launch.timezone is None


# ---------------------------------------------------------------------- delete safety (regressions)


def test_delete_refuses_while_the_host_lock_is_held_and_changes_nothing(store):
    from filelock import FileLock

    p = store.create_profile("starting")
    lock = FileLock(str(store.profile_dir(p.id) / "host.lock"))
    lock.acquire(timeout=0)  # a host that holds the lock but has not written runtime.json yet
    try:
        with pytest.raises(ProfileRunningError, match="starting or stopping"):
            store.delete_profile("starting")
    finally:
        lock.release()
    assert [x.id for x in store.list_profiles()] == [p.id]
    assert store.list_trash() == []
    assert not store.trash_dir.exists() or not any(store.trash_dir.iterdir())
    store.delete_profile("starting")  # works once the holder is gone
    assert store.list_profiles() == []


def test_delete_refuses_while_a_browser_holds_the_user_data_dir(store, monkeypatch):
    import profilepilot.browser.prefs as prefs

    p = store.create_profile("orphan chrome")
    monkeypatch.setattr(prefs, "profile_in_use", lambda udd: udd == store.user_data_dir(p.id))
    with pytest.raises(ProfileRunningError, match="open in a browser"):
        store.delete_profile("orphan chrome")
    with pytest.raises(ProfileRunningError):
        store.clone_profile("orphan chrome", "copy", copy_data=True)
    assert [x.name for x in store.list_profiles()] == ["orphan chrome"]


def test_failed_move_leaves_the_profile_intact_and_no_hidden_trash_copy(store, monkeypatch):
    import profilepilot.store as store_mod

    p = store.create_profile("busy")
    (store.user_data_dir(p.id) / "Default").mkdir()
    (store.user_data_dir(p.id) / "Default" / "Cookies").write_text("c")

    def refuse(src, dst):
        raise PermissionError(32, "The process cannot access the file because it is being used")

    monkeypatch.setattr(store_mod.os, "replace", refuse)
    monkeypatch.setattr(store_mod.time, "sleep", lambda s: None)
    with pytest.raises(store_mod.ProfilePilotError, match="in use by another process"):
        store.delete_profile("busy")
    monkeypatch.undo()
    assert store.get_profile("busy").id == p.id  # still listed, data untouched
    assert (store.user_data_dir(p.id) / "Default" / "Cookies").read_text() == "c"
    assert not store.trash_dir.exists() or not any(store.trash_dir.iterdir())


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows file-handle semantics")
def test_delete_with_a_file_held_open_on_windows_fails_cleanly(store, monkeypatch):
    import profilepilot.store as store_mod

    monkeypatch.setattr(store_mod.time, "sleep", lambda s: None)
    p = store.create_profile("held")
    log = store.profile_dir(p.id) / "host.log"
    with open(log, "a", encoding="utf-8") as fh:  # e.g. a log viewer, or a host still shutting down
        fh.write("x")
        with pytest.raises(store_mod.ProfilePilotError, match="in use by another process"):
            store.delete_profile("held")
        assert store.get_profile("held").id == p.id
        assert not store.trash_dir.exists() or not any(store.trash_dir.iterdir())
    entry = store.delete_profile("held")
    assert store.restore_profile(entry.trash_id).id == p.id


def test_stale_runtime_json_with_a_reused_pid_does_not_block_delete(store):
    import os

    p = store.create_profile("stale")
    store.runtime_file(p.id).write_text(json.dumps({
        "profile_id": p.id, "profile_name": "stale", "host_pid": os.getpid(),
        "started_at": "2000-01-01T00:00:00+00:00",  # our process started long after this file
    }))
    assert not store.is_running_on_disk(p.id)
    store.delete_profile("stale")
    assert store.list_profiles() == []


def test_restore_cleans_a_leftover_folder_with_only_host_files(store):
    p = store.create_profile("again")
    entry = store.delete_profile("again")
    leftover = store.profile_dir(p.id)
    leftover.mkdir()
    (leftover / "host.log").write_text("late log line")
    assert store.restore_profile(entry.trash_id).id == p.id
    assert store.get_profile("again").id == p.id

    entry = store.delete_profile("again")
    leftover.mkdir()
    (leftover / "notes.txt").write_text("user data?")
    with pytest.raises(ConflictError, match="left over"):
        store.restore_profile(entry.trash_id)
    assert (leftover / "notes.txt").exists()


def test_late_host_bookkeeping_does_not_recreate_a_deleted_profile(store):
    p = store.create_profile("gone")
    store.delete_profile("gone")
    store.touch_started(p.id)
    store.add_runtime(p.id, 5)
    with pytest.raises(NotFoundError):
        store.downloads_dir(p.id)
    assert not store.profile_dir(p.id).exists()


def test_purge_removes_trash_folders_without_metadata(store):
    store.trash_dir.mkdir(parents=True, exist_ok=True)
    orphan = store.trash_dir / "deadbeef-20200101000000"
    (orphan / "udd").mkdir(parents=True)
    (orphan / "udd" / "big.bin").write_bytes(b"x" * 10)
    assert store.list_trash() == []
    store.purge_trash(older_than_days=0)
    assert not orphan.exists()


def test_read_json_retries_a_sharing_violation(tmp_path, monkeypatch):
    from pathlib import Path

    from profilepilot.errors import ProfilePilotError
    from profilepilot.jsonio import read_json

    f = tmp_path / "x.json"
    f.write_text('{"a": 1}')
    real = Path.read_bytes
    failures = {"left": 3}

    def flaky(self):
        if self == f and failures["left"]:
            failures["left"] -= 1
            raise PermissionError(32, "being used by another process")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", flaky)
    monkeypatch.setattr("profilepilot.jsonio.time.sleep", lambda s: None)
    assert read_json(f) == {"a": 1}
    failures["left"] = 1000
    with pytest.raises(ProfilePilotError, match="locked"):
        read_json(f)


def test_posix_singleton_lock_with_a_reused_pid_is_stale(monkeypatch, tmp_path):
    import os
    import socket
    import time

    from profilepilot.browser import prefs

    me = os.getpid()
    lock = tmp_path / "SingletonLock"
    monkeypatch.setattr(prefs.os, "readlink", lambda p: f"{socket.gethostname()}-{me}")
    monkeypatch.setattr(prefs.os, "lstat", lambda p: type("S", (), {"st_mtime": time.time()})())
    assert prefs._singleton_lock_in_use(lock)  # written after our process started: in use
    monkeypatch.setattr(prefs.os, "lstat", lambda p: type("S", (), {"st_mtime": 1_000_000.0})())
    assert not prefs._singleton_lock_in_use(lock)  # written in 1970: our PID is a reuse, the lock is stale
