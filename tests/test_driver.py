"""The CDP driver module (profilepilot.automation.driver): selection, error classes, worlds and the
patchright driver patch (no faked focus). No browser needed."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from profilepilot.automation import driver
from profilepilot.errors import ProfilePilotError
from profilepilot.models import AppConfig

needs_patchright = pytest.mark.skipif(not driver.installed("patchright"), reason="patchright is not installed")


def _config(name: str) -> AppConfig:
    return AppConfig.model_validate({"automation": {"driver": name}})


def test_selection_order_env_then_config_then_patchright(monkeypatch):
    monkeypatch.delenv(driver.ENV_DRIVER, raising=False)
    auto = "patchright" if driver.installed("patchright") else "playwright"
    assert driver.select_driver(None) == auto
    assert driver.select_driver(AppConfig()) == auto  # automation.driver defaults to "auto"
    assert driver.select_driver(_config("playwright")) == "playwright"
    monkeypatch.setenv(driver.ENV_DRIVER, "Playwright ")
    assert driver.select_driver(_config("auto")) == "playwright"  # the environment wins
    monkeypatch.setenv(driver.ENV_DRIVER, "auto")
    assert driver.select_driver(_config("playwright")) == "playwright"  # env "auto" leaves it to the config


def test_unknown_or_missing_drivers_are_errors_not_silent_fallbacks(monkeypatch):
    monkeypatch.setenv(driver.ENV_DRIVER, "puppeteer")
    with pytest.raises(ProfilePilotError, match="Unknown automation driver 'puppeteer'"):
        driver.select_driver(None)
    monkeypatch.setenv(driver.ENV_DRIVER, "patchright")
    monkeypatch.setattr(driver, "installed", lambda name: name != "patchright")
    with pytest.raises(ProfilePilotError, match="'patchright' is not installed"):
        driver.select_driver(None)
    monkeypatch.delenv(driver.ENV_DRIVER)
    assert driver.select_driver(None) == "playwright"  # auto without patchright
    with pytest.raises(Exception):
        _config("selenium")  # the config model only takes known names


def test_error_tuples_catch_both_packages():
    from playwright._impl._errors import Error as PlaywrightError
    from playwright._impl._errors import TimeoutError as PlaywrightTimeoutError

    errors = [PlaywrightError("x"), PlaywrightTimeoutError("t")]
    if driver.installed("patchright"):
        from patchright._impl._errors import Error as PatchrightError
        from patchright._impl._errors import TimeoutError as PatchrightTimeoutError

        errors += [PatchrightError("x"), PatchrightTimeoutError("t")]
        assert isinstance(PatchrightTimeoutError("t"), driver.TimeoutError)
    assert isinstance(PlaywrightTimeoutError("t"), driver.TimeoutError)
    assert all(isinstance(cls, type) and issubclass(cls, Exception) for cls in driver.Error + driver.TimeoutError)
    for exc in errors:
        with pytest.raises(ValueError):  # caught and re-raised: the tuple works in except clauses
            try:
                raise exc
            except (*driver.Error, KeyError):
                raise ValueError from None


def test_world_kwargs_follow_the_package_of_the_object():
    class FakePatchrightPage:
        pass

    FakePatchrightPage.__module__ = "patchright.async_api._generated"

    class FakePlaywrightPage:
        pass

    FakePlaywrightPage.__module__ = "playwright.async_api._generated"
    assert driver.driver_of(FakePatchrightPage()) == "patchright"
    assert driver.world_kwargs(FakePatchrightPage(), "isolated") == {"isolated_context": True}
    assert driver.world_kwargs(FakePatchrightPage(), "main") == {"isolated_context": False}
    assert driver.world_kwargs(FakePlaywrightPage(), "isolated") == {} == driver.world_kwargs(FakePlaywrightPage(), "main")
    with pytest.raises(ValueError):
        driver.world_kwargs(FakePatchrightPage(), "page")  # type: ignore[arg-type]


# ---------------------------------------------------------------------- the patchright driver patch


@needs_patchright
def test_the_patch_matches_the_installed_patchright():
    driver.check_patchright_patches()  # raises when the bundle text moved (another patchright version)
    patches = {p["name"]: p for p in driver.patchright_patches()}
    assert list(patches) == ["focus-emulation-no-defaults", "evaluate-without-user-gesture"]
    bundle = (driver.patchright_driver_lib() / "coreBundle.js").read_text(encoding="utf-8")
    # the replacement uses a name that exists in that scope of the bundle
    at = bundle.index(patches["focus-emulation-no-defaults"]["find"])
    assert "const skipDefaultOverrides = browserOptions.noDefaults" in bundle[at - 600:at]
    # the user gesture is the one every evaluate sends: Runtime.callFunctionOn in evaluateWithArguments,
    # where the replacement's names exist (the handle's context, its frame's page, browser context, browser)
    at = bundle.index(patches["evaluate-without-user-gesture"]["find"])
    before, after = bundle[at - 700:at], bundle[at:at + 400]
    assert "async evaluateWithArguments(expression2, returnByValue, utilityScript, values, handles)" in before
    assert before.rfind('"Runtime.callFunctionOn"') > before.rfind("async evaluateWithArguments(")
    assert "createHandle(utilityScript._context, remoteObject)" in after
    for name in ("this.frame = frame;", "this.browserContext = browserContext;", "this._defaultContext = null;",
                 "this.options = options;"):
        assert name in bundle, name


@needs_patchright
def test_a_bundle_that_does_not_match_is_refused(monkeypatch, tmp_path):
    (tmp_path / "coreBundle.js").write_text("// another patchright version\n", encoding="utf-8")
    monkeypatch.setattr(driver, "patchright_driver_lib", lambda: tmp_path)
    with pytest.raises(ProfilePilotError, match="does not match the installed patchright"):
        driver.check_patchright_patches()


@needs_patchright
def test_patchright_drivers_start_with_the_preload():
    driver._install_patchright_preload()
    driver._install_patchright_preload()  # idempotent
    from patchright._impl import _transport

    options = _transport.get_driver_env()["NODE_OPTIONS"]
    assert options.count(f'--require "{driver.PRELOAD.as_posix()}"') == 1


def _node() -> str:
    path = driver.patchright_driver_lib().parent.parent / ("node.exe" if os.name == "nt" else "node")
    if not path.is_file():
        pytest.skip("patchright's node is not available")
    return str(path)


def _fake_bundle(root: Path, find: str) -> Path:
    """A stand-in for patchright/driver/package/lib/coreBundle.js around the patched texts: it exports
    whether focus would be emulated, and the userGesture of an evaluate on the default context of a
    ``no_defaults`` connection and on another connection."""
    lib = root / "patchright" / "driver" / "package" / "lib"
    lib.mkdir(parents=True)
    path = lib / "coreBundle.js"
    path.write_text(
        "const skipDefaultOverrides = true;\n"
        "const self = {_isMainFrame: () => true, _crPage: {_browserContext: {_options: {}}}};\n"
        f"const focus = (function () {{ {find} return 'focus emulated'; return 'native'; }}).call(self);\n"
        "function gesture(noDefaults) {\n"
        "  const browser = {options: {noDefaults}, _defaultContext: null};\n"
        "  browser._defaultContext = {_browser: browser};\n"
        "  const utilityScript = {_context: {frame: {_page: {browserContext: browser._defaultContext}}}};\n"
        "  const params = {awaitPromise: true,\n          userGesture: true\n        };\n"
        "  return params.userGesture;\n"
        "}\n"
        "module.exports = `${focus} / userGesture ${gesture(true)}, otherwise ${gesture(false)}`;\n",
        encoding="utf-8",
    )
    return path


@needs_patchright
@pytest.mark.parametrize("preload", [False, True])
def test_the_preload_restores_upstreams_rule_in_node(tmp_path, preload):
    """Run the patched texts in patchright's own node: with the preload a no_defaults default context
    gets no focus emulation and evaluates carry no user gesture; without it (patchright as shipped)
    focus is emulated and every evaluate is a user gesture."""
    bundle = _fake_bundle(tmp_path, driver.patchright_patches()[0]["find"])
    env = {**os.environ, "NODE_OPTIONS": f'--require "{driver.PRELOAD.as_posix()}"' if preload else ""}
    out = subprocess.run([_node(), "-e", f"console.log(require({bundle.as_posix()!r}))"], capture_output=True,
                         text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ("native / userGesture false, otherwise true" if preload
                                  else "focus emulated / userGesture true, otherwise true")


@needs_patchright
def test_the_preload_stops_a_driver_it_cannot_patch(tmp_path):
    bundle = _fake_bundle(tmp_path, "if (this._isMainFrame())")
    env = {**os.environ, "NODE_OPTIONS": f'--require "{driver.PRELOAD.as_posix()}"'}
    out = subprocess.run([_node(), "-e", f"require({bundle.as_posix()!r})"], capture_output=True, text=True,
                         env=env, timeout=60)
    assert out.returncode != 0 and "does not apply" in out.stderr
