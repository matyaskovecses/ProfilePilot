"""On-disk store for profiles, proxies, trash and app config.

Layout (``root`` defaults to :func:`profilepilot.paths.data_root`)::

    config.json                      AppConfig
    proxies.json                     {"proxies": [ProxyRecord, ...]}   (no passwords)
    identities.json                  autofill identities (no sensitive values; see identity.py)
    secrets.json                     fallback secret store (only if no OS keyring)
    clipboard.lock                   serialises type-paste through the system clipboard
    profiles/<id>/profile.json       Profile
    profiles/<id>/udd/               Chrome --user-data-dir (cookies, history, storage, cache)
    profiles/<id>/runtime.json       RuntimeInfo while running (written by the host process)
    profiles/<id>/host.log           host process log
    profiles/<id>/downloads/         downloads
    trash/<trash_id>/                deleted profile directories (restorable)

All writes are atomic and read-modify-write cycles are guarded by cross-process file locks, so
several MCP servers (Claude Desktop, Claude Code, ChatGPT) can share one store safely.
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from filelock import FileLock, Timeout
from pydantic import ValidationError

from .errors import AmbiguousError, ConflictError, NotFoundError, ProfilePilotError, ProfileRunningError
from .jsonio import lock_for, read_json, write_json
from .models import AppConfig, LaunchOptions, Profile, ProxyCheck, ProxyRecord, RuntimeInfo, TrashEntry, utcnow
from .paths import data_root, is_valid_id, new_id
from .procs import host_alive, pid_started_before
from .proxy.url import ProxyEndpoint, parse_proxy
from .secrets import SecretStore

# Chrome data that is safe to skip when cloning / archiving (pure caches).
CACHE_DIRS = {
    "Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
    "GrShaderCache", "GraphiteDawnCache", "ShaderCache", "Crashpad", "component_crx_cache",
    "optimization_guide_model_store", "Safe Browsing", "segmentation_platform", "BrowserMetrics",
}
RUNTIME_FILES = {"lockfile", "DevToolsActivePort", "SingletonLock", "SingletonSocket", "SingletonCookie"}
HOST_LOCK_NAME = "host.lock"
# Files a host process leaves in profiles/<id>/ itself (safe to discard in an orphaned folder).
HOST_LEFTOVERS = {HOST_LOCK_NAME, "host.log", "host.log.1", "host-stderr.log", "runtime.json"}
MAX_NAME = 64


def _launch_options(data: LaunchOptions | dict[str, Any] | None) -> LaunchOptions:
    """Build LaunchOptions, turning pydantic validation errors into a readable ProfilePilotError."""
    if isinstance(data, LaunchOptions):
        return data
    try:
        return LaunchOptions.model_validate(data or {})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'launch'}: {err['msg'].removeprefix('Value error, ')}"
            for err in exc.errors()
        )
        raise ProfilePilotError(f"Invalid launch options - {problems}") from None


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ProfilePilotError("Name must not be empty.")
    if len(name) > MAX_NAME:
        raise ProfilePilotError(f"Name is too long (max {MAX_NAME} characters).")
    if any(ord(ch) < 32 for ch in name):
        raise ProfilePilotError("Name must not contain control characters.")
    return name


class Store:
    def __init__(self, root: Path | str | None = None, secrets: SecretStore | None = None) -> None:
        self.root = Path(root) if root else data_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.profiles_dir = self.root / "profiles"
        self.trash_dir = self.root / "trash"
        self.proxies_file = self.root / "proxies.json"
        self.config_file = self.root / "config.json"
        self.clipboard_lock = self.root / "clipboard.lock"
        """Cross-process lock serialising type-paste (see :mod:`profilepilot.automation.clipboard`)."""
        self.profiles_dir.mkdir(exist_ok=True)
        self.secrets = secrets or SecretStore(self.root)

    # ------------------------------------------------------------------ paths

    def profile_dir(self, profile_id: str) -> Path:
        if not is_valid_id(profile_id):
            raise ProfilePilotError(f"Invalid profile id: {profile_id!r}")
        return self.profiles_dir / profile_id

    def user_data_dir(self, profile_id: str) -> Path:
        return self.profile_dir(profile_id) / "udd"

    def runtime_file(self, profile_id: str) -> Path:
        return self.profile_dir(profile_id) / "runtime.json"

    def host_log(self, profile_id: str) -> Path:
        return self.profile_dir(profile_id) / "host.log"

    def downloads_dir(self, profile_id: str) -> Path:
        base = self.profile_dir(profile_id)
        if not base.is_dir():  # never resurrect the folder of a deleted profile (it blocks a restore)
            raise NotFoundError(f"Profile {profile_id} no longer exists.")
        path = base / "downloads"
        path.mkdir(exist_ok=True)
        return path

    def _profile_file(self, profile_id: str) -> Path:
        return self.profile_dir(profile_id) / "profile.json"

    @property
    def _profiles_lock(self):
        return lock_for(self.profiles_dir / "index")

    # ------------------------------------------------------------------ config

    def load_config(self) -> AppConfig:
        return AppConfig.model_validate(read_json(self.config_file, {}) or {})

    def save_config(self, config: AppConfig) -> None:
        with lock_for(self.config_file):
            write_json(self.config_file, config.model_dump(mode="json"))

    # ------------------------------------------------------------------ profiles

    def list_profiles(self, tag: str | None = None) -> list[Profile]:
        profiles: list[Profile] = []
        if not self.profiles_dir.exists():
            return profiles
        for entry in self.profiles_dir.iterdir():
            if not entry.is_dir() or not is_valid_id(entry.name):
                continue
            data = read_json(entry / "profile.json")
            if not data:
                continue
            try:
                profile = Profile.model_validate(data)
            except Exception:
                continue
            if tag and tag.lower() not in (t.lower() for t in profile.tags):
                continue
            profiles.append(profile)
        profiles.sort(key=lambda p: (p.name.casefold(), p.id))
        return profiles

    def get_profile(self, ref: str) -> Profile:
        """Resolve a profile by id, name (case-insensitive) or unique id prefix (>= 3 chars)."""
        ref = (ref or "").strip()
        if not ref:
            raise NotFoundError("No profile given.")
        if is_valid_id(ref.lower()):
            data = read_json(self._profile_file(ref.lower()))
            if data:
                return Profile.model_validate(data)
        profiles = self.list_profiles()
        by_name = [p for p in profiles if p.name.casefold() == ref.casefold()]
        if len(by_name) == 1:
            return by_name[0]
        if len(ref) >= 3:
            by_prefix = [p for p in profiles if p.id.startswith(ref.lower())]
            if len(by_prefix) == 1:
                return by_prefix[0]
            if len(by_prefix) > 1:
                raise AmbiguousError(f"'{ref}' matches several profiles: " + ", ".join(f"{p.name} ({p.id})" for p in by_prefix))
        names = ", ".join(p.name for p in profiles[:20]) or "none yet"
        raise NotFoundError(f"Profile '{ref}' not found. Existing profiles: {names}.")

    def create_profile(
        self,
        name: str,
        *,
        notes: str = "",
        tags: Iterable[str] = (),
        proxy_id: str | None = None,
        browser: str = "auto",
        launch: LaunchOptions | dict[str, Any] | None = None,
        color: str | None = None,
        identity_id: str | None = None,
    ) -> Profile:
        """Create a profile. ``proxy_id`` / ``identity_id`` accept a name, id or unique id prefix."""
        name = _validate_name(name)
        if proxy_id:
            proxy_id = self.get_proxy(proxy_id).id
        identity_id = self._identity_id(identity_id)
        with self._profiles_lock:
            if any(p.name.casefold() == name.casefold() for p in self.list_profiles()):
                raise ConflictError(f"A profile named '{name}' already exists.")
            pid = new_id()
            while self.profile_dir(pid).exists():
                pid = new_id()
            launch_opts = _launch_options(launch)
            if launch is None:
                launch_opts.window = self.load_config().default_window
            profile = Profile(
                id=pid, name=name, notes=notes, tags=_clean_tags(tags), proxy_id=proxy_id,
                browser=browser or "auto", launch=launch_opts, color=color, identity_id=identity_id,
            )
            self.user_data_dir(pid).mkdir(parents=True, exist_ok=True)
            write_json(self._profile_file(pid), profile.model_dump(mode="json"))
        return profile

    def save_profile(self, profile: Profile, *, expected_rev: int | None = None) -> Profile:
        """Persist ``profile`` (bumping ``rev``). ``expected_rev`` enables optimistic concurrency."""
        path = self._profile_file(profile.id)
        with lock_for(path):
            current = read_json(path)
            if current is None:
                raise NotFoundError(f"Profile {profile.id} no longer exists.")
            if expected_rev is not None and current.get("rev") != expected_rev:
                raise ConflictError(
                    f"Profile '{profile.name}' was modified concurrently (rev {current.get('rev')} != {expected_rev}). Re-read and retry."
                )
            profile.rev = int(current.get("rev", 1)) + 1
            profile.updated_at = utcnow()
            write_json(path, profile.model_dump(mode="json"))
        return profile

    def update_profile(self, ref: str, *, expected_rev: int | None = None, **changes: Any) -> Profile:
        """Update fields. Supported keys: name, notes, tags, color, proxy_id, identity_id, browser,
        launch (dict, merged). ``proxy_id`` / ``identity_id`` take a name, id or id prefix; None or
        ``""`` removes the link."""
        with self._profiles_lock:
            profile = self.get_profile(ref)
            if "identity_id" in changes:
                profile.identity_id = self._identity_id(changes.pop("identity_id"))
            if "name" in changes and changes["name"] is not None:
                new_name = _validate_name(changes.pop("name"))
                if new_name.casefold() != profile.name.casefold() and any(
                    p.name.casefold() == new_name.casefold() for p in self.list_profiles()
                ):
                    raise ConflictError(f"A profile named '{new_name}' already exists.")
                profile.name = new_name
            if "proxy_id" in changes:
                proxy_ref = changes.pop("proxy_id")
                profile.proxy_id = self.get_proxy(proxy_ref).id if proxy_ref else None
            if "tags" in changes and changes["tags"] is not None:
                profile.tags = _clean_tags(changes.pop("tags"))
            if "launch" in changes and changes["launch"] is not None:
                patch = changes.pop("launch")
                if isinstance(patch, LaunchOptions):
                    patch = patch.model_dump()
                merged = profile.launch.model_dump() | dict(patch)
                profile.launch = _launch_options(merged)
            for key in ("notes", "color", "browser"):
                if key in changes and changes[key] is not None:
                    setattr(profile, key, changes.pop(key))
            unknown = [k for k, v in changes.items() if v is not None]
            if unknown:
                raise ProfilePilotError(f"Unknown profile field(s): {', '.join(unknown)}")
            return self.save_profile(profile, expected_rev=expected_rev)

    def _identity_id(self, ref: str | None) -> str | None:
        """The id of identity ``ref`` (name, id or unique id prefix); None for None / ``""``."""
        ref = (ref or "").strip()
        if not ref:
            return None
        from .identity import IdentityStore  # lazy: identity.py is not needed by most store users

        return IdentityStore(self).get(ref).id

    def profiles_using_identity(self, identity_id: str) -> list[Profile]:
        return [p for p in self.list_profiles() if p.identity_id == identity_id]

    def touch_started(self, profile_id: str) -> None:
        path = self._profile_file(profile_id)
        if not path.parent.is_dir():
            return  # deleted meanwhile: lock_for() would recreate the folder and block a restore
        with lock_for(path):
            data = read_json(path)
            if data:
                data["last_started_at"] = utcnow().isoformat()
                write_json(path, data)

    def add_runtime(self, profile_id: str, seconds: float) -> None:
        path = self._profile_file(profile_id)
        if not path.parent.is_dir():
            return  # deleted meanwhile (see touch_started)
        with lock_for(path):
            data = read_json(path)
            if data:
                data["total_runtime_s"] = int(data.get("total_runtime_s", 0)) + int(max(0, seconds))
                write_json(path, data)

    def is_running_on_disk(self, profile_id: str) -> bool:
        """Cheap check: a runtime.json whose host process is still alive (PID-reuse safe)."""
        path = self.runtime_file(profile_id)
        data = read_json(path)
        if not data or not isinstance(data, dict):
            return False
        try:
            return host_alive(RuntimeInfo.model_validate(data))
        except ValidationError:
            # Malformed: the host wrote the file after it started, so it must predate the mtime.
            try:
                return pid_started_before(int(data.get("host_pid") or 0), path.stat().st_mtime)
            except (OSError, TypeError, ValueError):
                return False

    def _ensure_not_in_use(self, profile: Profile, action: str) -> None:
        """Refuse when anything still holds the profile: a live host (registered or not) or a
        browser process on its user-data-dir."""
        if self.is_running_on_disk(profile.id):
            raise ProfileRunningError(f"Profile '{profile.name}' is running; stop it before you {action}.")
        lock_path = self.profile_dir(profile.id) / HOST_LOCK_NAME
        if lock_path.exists():
            # Probe the host lock and release it at once: holding it would block the move itself.
            lock = FileLock(str(lock_path))
            try:
                lock.acquire(timeout=0)
            except Timeout:
                raise ProfileRunningError(
                    f"Profile '{profile.name}' is starting or stopping; try again in a moment."
                ) from None
            except OSError:
                pass
            else:
                lock.release()
        from .browser.prefs import profile_in_use  # lazy: keeps `import profilepilot.store` light

        if profile_in_use(self.user_data_dir(profile.id)):
            raise ProfileRunningError(
                f"Profile '{profile.name}' is open in a browser process; close it before you {action}."
            )

    def clone_profile(self, ref: str, new_name: str, *, copy_data: bool = False) -> Profile:
        src = self.get_profile(ref)
        if copy_data:
            self._ensure_not_in_use(src, "clone its browser data")
        identity_id = src.identity_id
        if identity_id:
            try:
                self._identity_id(identity_id)
            except NotFoundError:  # the linked identity was deleted meanwhile: do not copy a dead link
                identity_id = None
        clone = self.create_profile(
            new_name, notes=src.notes, tags=src.tags, proxy_id=src.proxy_id, browser=src.browser,
            launch=src.launch.model_copy(deep=True), color=src.color, identity_id=identity_id,
        )
        if copy_data:
            dst = self.user_data_dir(clone.id)
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(self.user_data_dir(src.id), dst, ignore=_ignore_volatile)
        return clone

    def delete_profile(self, ref: str) -> TrashEntry:
        """Move a stopped profile to the trash (restorable with :meth:`restore_profile`)."""
        with self._profiles_lock:
            profile = self.get_profile(ref)
            self._ensure_not_in_use(profile, "delete it")
            self.trash_dir.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            trash_id = f"{profile.id}-{stamp}"
            target = self.trash_dir / trash_id
            src = self.profile_dir(profile.id)
            _remove_volatile(src)
            size = _dir_size(src)
            _move_dir(src, target)
            entry = TrashEntry(trash_id=trash_id, profile_id=profile.id, name=profile.name, deleted_at=utcnow(), size_bytes=size)
            write_json(target / "trash.json", entry.model_dump(mode="json"))
        return entry

    def list_trash(self) -> list[TrashEntry]:
        entries: list[TrashEntry] = []
        if not self.trash_dir.exists():
            return entries
        for d in self.trash_dir.iterdir():
            data = read_json(d / "trash.json") if d.is_dir() else None
            if data:
                entries.append(TrashEntry.model_validate(data))
        entries.sort(key=lambda e: e.deleted_at, reverse=True)
        return entries

    def _orphaned_trash_dirs(self) -> list[Path]:
        """``trash/*`` folders without a ``trash.json`` (left by an interrupted delete)."""
        if not self.trash_dir.exists():
            return []
        return [d for d in self.trash_dir.iterdir() if d.is_dir() and not (d / "trash.json").exists()]

    def restore_profile(self, trash_id: str) -> Profile:
        with self._profiles_lock:
            src = self.trash_dir / trash_id
            entry_data = read_json(src / "trash.json")
            if not entry_data:
                raise NotFoundError(f"Trash entry '{trash_id}' not found.")
            entry = TrashEntry.model_validate(entry_data)
            pid = entry.profile_id
            target = self.profile_dir(pid)
            if target.exists():
                if self._profile_file(pid).exists():
                    raise ConflictError(f"A profile with id {pid} already exists.")
                leftovers = {p.name for p in target.iterdir()} if target.is_dir() else {"?"}
                if leftovers - HOST_LEFTOVERS:
                    raise ConflictError(
                        f"Cannot restore '{entry.name}': the folder {target} is left over from an earlier "
                        "run and still contains files. Move or delete it, then try again."
                    )
                _rmtree(target)  # only host logs / lock files: safe to discard
                if target.exists():
                    raise ConflictError(
                        f"Cannot restore '{entry.name}': the leftover folder {target} is in use by another "
                        "process. Close it and try again."
                    )
            _move_dir(src, target)
            (self.profile_dir(pid) / "trash.json").unlink(missing_ok=True)
            profile = Profile.model_validate(read_json(self._profile_file(pid)))
            taken = {p.name.casefold() for p in self.list_profiles() if p.id != pid}
            if profile.name.casefold() in taken:
                base, n = profile.name, 2
                while f"{base} ({n})".casefold() in taken:
                    n += 1
                profile.name = f"{base} ({n})"[:MAX_NAME]
                write_json(self._profile_file(pid), profile.model_dump(mode="json"))
            return profile

    def purge_trash(self, older_than_days: float = 7.0) -> int:
        cutoff = utcnow() - timedelta(days=older_than_days)
        removed = 0
        for entry in self.list_trash():
            if entry.deleted_at <= cutoff:
                _rmtree(self.trash_dir / entry.trash_id)
                removed += 1
        # Folders without trash.json are invisible to list_trash / restore: never keep them.
        orphan_cutoff = time.time() - older_than_days * 86400
        for orphan in self._orphaned_trash_dirs():
            try:
                # older_than_days <= 0 means "everything": a just-created folder's NTFS mtime can be a
                # few microseconds *after* time.time(), so never rely on the comparison for that case.
                if older_than_days <= 0 or orphan.stat().st_mtime <= orphan_cutoff:
                    _rmtree(orphan)
            except OSError:
                pass
        return removed

    # ------------------------------------------------------------------ proxies

    def _load_proxies(self) -> list[ProxyRecord]:
        data = read_json(self.proxies_file, {}) or {}
        out = []
        for item in data.get("proxies", []):
            try:
                out.append(ProxyRecord.model_validate(item))
            except Exception:
                continue
        return out

    def _save_proxies(self, proxies: list[ProxyRecord]) -> None:
        write_json(self.proxies_file, {"proxies": [p.model_dump(mode="json") for p in proxies]})

    def list_proxies(self, tag: str | None = None) -> list[ProxyRecord]:
        proxies = self._load_proxies()
        if tag:
            proxies = [p for p in proxies if tag.lower() in (t.lower() for t in p.tags)]
        return sorted(proxies, key=lambda p: (p.name.casefold(), p.id))

    def get_proxy(self, ref: str) -> ProxyRecord:
        ref = (ref or "").strip()
        proxies = self._load_proxies()
        for p in proxies:
            if p.id == ref.lower():
                return p
        by_name = [p for p in proxies if p.name.casefold() == ref.casefold()]
        if len(by_name) == 1:
            return by_name[0]
        if len(ref) >= 3:
            by_prefix = [p for p in proxies if p.id.startswith(ref.lower())]
            if len(by_prefix) == 1:
                return by_prefix[0]
            if len(by_prefix) > 1:
                raise AmbiguousError(f"'{ref}' matches several proxies: " + ", ".join(p.name for p in by_prefix))
        raise NotFoundError(f"Proxy '{ref}' not found. Use proxy_list to see saved proxies.")

    def add_proxy(
        self,
        spec: str | ProxyEndpoint,
        name: str | None = None,
        *,
        default_scheme: str = "http",
        tags: Iterable[str] = (),
        notes: str = "",
    ) -> ProxyRecord:
        """Save a proxy (any format accepted by :func:`parse_proxy`).

        Re-adding an existing scheme/host/port/username returns the existing record (updating
        its password if one was given).
        """
        endpoint = spec if isinstance(spec, ProxyEndpoint) else parse_proxy(spec, default_scheme)
        with lock_for(self.proxies_file):
            proxies = self._load_proxies()
            for existing in proxies:
                if (existing.scheme, existing.host.lower(), existing.port, existing.username) == (
                    endpoint.scheme, endpoint.host.lower(), endpoint.port, endpoint.username
                ):
                    if endpoint.password is not None:
                        self.secrets.set(f"proxy:{existing.id}", endpoint.password)
                        existing.has_password = True
                        self._save_proxies(proxies)
                    return existing
            taken_ids = {p.id for p in proxies}
            pid = new_id()
            while pid in taken_ids:
                pid = new_id()
            final_name = _validate_name(name) if name else f"{endpoint.host}:{endpoint.port}"
            taken_names = {p.name.casefold() for p in proxies}
            if final_name.casefold() in taken_names:
                if name:
                    raise ConflictError(f"A proxy named '{final_name}' already exists.")
                final_name = f"{final_name}#{pid[:4]}"
            record = ProxyRecord(
                id=pid, name=final_name, scheme=endpoint.scheme, host=endpoint.host, port=endpoint.port,
                username=endpoint.username, has_password=endpoint.password is not None,
                tags=_clean_tags(tags), notes=notes,
            )
            if endpoint.password is not None:
                self.secrets.set(f"proxy:{pid}", endpoint.password)
            proxies.append(record)
            self._save_proxies(proxies)
        return record

    def import_proxies(self, text: str, *, default_scheme: str = "http", tags: Iterable[str] = ()) -> tuple[list[ProxyRecord], list[str]]:
        """Bulk import, one proxy per line. ``# comment`` lines are skipped and a trailing
        ``  # name`` names the proxy. Returns (records, errors)."""
        records: list[ProxyRecord] = []
        errors: list[str] = []
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = None
            if " #" in line:
                line, _, name = line.partition(" #")
                line, name = line.strip(), name.strip() or None
            try:
                records.append(self.add_proxy(line, name, default_scheme=default_scheme, tags=tags))
            except Exception as exc:
                errors.append(f"line {lineno}: {exc}")
        return records, errors

    def update_proxy(self, ref: str, **changes: Any) -> ProxyRecord:
        """Update name/tags/notes, or replace the endpoint with ``url=...`` (keeps the id)."""
        with lock_for(self.proxies_file):
            proxies = self._load_proxies()
            target = self.get_proxy(ref)
            record = next(p for p in proxies if p.id == target.id)
            if changes.get("url"):
                ep = parse_proxy(changes.pop("url"), changes.pop("default_scheme", None) or record.scheme)
                record.scheme, record.host, record.port, record.username = ep.scheme, ep.host, ep.port, ep.username
                if ep.password is not None:
                    self.secrets.set(f"proxy:{record.id}", ep.password)
                    record.has_password = True
                else:
                    self.secrets.delete(f"proxy:{record.id}")
                    record.has_password = False
                record.last_check = None
            changes.pop("url", None)
            changes.pop("default_scheme", None)
            if changes.get("name"):
                new_name = _validate_name(changes.pop("name"))
                if any(p.name.casefold() == new_name.casefold() and p.id != record.id for p in proxies):
                    raise ConflictError(f"A proxy named '{new_name}' already exists.")
                record.name = new_name
            if changes.get("tags") is not None:
                record.tags = _clean_tags(changes.pop("tags"))
            if changes.get("notes") is not None:
                record.notes = changes.pop("notes")
            unknown = [k for k, v in changes.items() if v is not None]
            if unknown:
                raise ProfilePilotError(f"Unknown proxy field(s): {', '.join(unknown)}")
            self._save_proxies(proxies)
            return record

    def set_proxy_check(self, ref: str, check: ProxyCheck) -> None:
        with lock_for(self.proxies_file):
            proxies = self._load_proxies()
            target = self.get_proxy(ref)
            for p in proxies:
                if p.id == target.id:
                    p.last_check = check
            self._save_proxies(proxies)

    def remove_proxy(self, ref: str, *, force: bool = False) -> list[str]:
        """Delete a proxy. Returns names of profiles that were unbound (requires ``force``)."""
        target = self.get_proxy(ref)
        bound = [p for p in self.list_profiles() if p.proxy_id == target.id]
        if bound and not force:
            raise ConflictError(
                f"Proxy '{target.name}' is used by: {', '.join(p.name for p in bound)}. Pass force=true to unbind them."
            )
        for p in bound:
            self.update_profile(p.id, proxy_id=None)
        with lock_for(self.proxies_file):
            proxies = [p for p in self._load_proxies() if p.id != target.id]
            self._save_proxies(proxies)
        self.secrets.delete(f"proxy:{target.id}")
        return [p.name for p in bound]

    def proxy_endpoint(self, ref: str) -> ProxyEndpoint:
        """Full endpoint including the password (for the relay only - never show to a model)."""
        record = self.get_proxy(ref)
        password = self.secrets.get(f"proxy:{record.id}") if record.has_password else None
        return ProxyEndpoint(record.scheme, record.host, record.port, record.username, password)

    def profile_proxy_endpoint(self, profile: Profile) -> ProxyEndpoint | None:
        return self.proxy_endpoint(profile.proxy_id) if profile.proxy_id else None


# ---------------------------------------------------------------------- helpers


def _clean_tags(tags: Iterable[str] | None) -> list[str]:
    seen: list[str] = []
    for tag in tags or ():
        tag = str(tag).strip()
        if tag and tag.casefold() not in (t.casefold() for t in seen):
            seen.append(tag[:32])
    return seen


def _ignore_volatile(directory: str, names: list[str]) -> set[str]:
    return {n for n in names if n in CACHE_DIRS or n in RUNTIME_FILES}


def _remove_volatile(profile_dir: Path) -> None:
    udd = profile_dir / "udd"
    if not udd.exists():
        return
    for path in [udd, *(p for p in udd.iterdir() if p.is_dir())]:
        for name in CACHE_DIRS:
            target = path / name
            if target.is_dir():
                _rmtree(target)
    for name in RUNTIME_FILES:
        (udd / name).unlink(missing_ok=True)
    (profile_dir / "runtime.json").unlink(missing_ok=True)


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _on_rm_error(func, path, _exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, onerror=_on_rm_error)


def _move_dir(src: Path, dst: Path) -> None:
    """Rename ``src`` to ``dst`` (both inside the data root, so never across volumes).

    There is deliberately no copy-and-delete fallback: when a file in ``src`` is held open, a copy
    would succeed while the delete half-fails, leaving a profile that is neither here nor there.
    On failure nothing is changed and a clear error is raised.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:  # antivirus / indexer, or a real holder: retry briefly
            if attempt == 9:
                break
            time.sleep(0.2)
        except OSError:
            break
    raise ProfilePilotError(
        f"Cannot move {src.name}: files in the profile folder are in use by another process (a starting/"
        "stopping browser host, a terminal or a log viewer). Close it and try again."
    )
