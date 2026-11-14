"""The hand-off release: config pinned to upstream's Kingdom backend, updates taken from upstream."""

from __future__ import annotations

import configparser

import semver

from p99_sso_login_proxy import config, updater


def _cfg(text: str = "") -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    cfg.read_string(text)
    return cfg


def test_typical_fork_config_gets_upstream_backend_name_and_token():
    cfg = _cfg("[DEFAULT]\nuser_api_token = tok\n\n[api_tokens]\nKingdom Login Proxy = tok\n")

    assert config.pin_kingdom_backend(cfg) is True

    assert cfg.get("DEFAULT", "sso_api_name") == "Kingdom"
    assert cfg.get("DEFAULT", "sso_api") == "https://bot.kingdomdkp.com"
    assert cfg.get("api_tokens", "Kingdom") == "tok"
    # Kept for rollback to an earlier fork build.
    assert cfg.get("api_tokens", "Kingdom Login Proxy") == "tok"


def test_legacy_backend_name_is_renamed():
    cfg = _cfg(
        "[DEFAULT]\nsso_api_name = Kingdom Login Proxy\nsso_api = https://bot.kingdomdkp.com\n\n"
        "[api_tokens]\nKingdom Login Proxy = tok\n"
    )

    assert config.pin_kingdom_backend(cfg) is True
    assert cfg.get("DEFAULT", "sso_api_name") == "Kingdom"
    assert cfg.get("api_tokens", "Kingdom") == "tok"


def test_token_only_in_default_section_is_copied():
    cfg = _cfg("[DEFAULT]\nuser_api_token = tok\n")

    assert config.pin_kingdom_backend(cfg) is True
    assert cfg.get("api_tokens", "Kingdom") == "tok"


def test_empty_config_is_pinned_without_inventing_a_token():
    cfg = _cfg()

    assert config.pin_kingdom_backend(cfg) is True
    assert cfg.get("DEFAULT", "sso_api_name") == "Kingdom"
    assert not cfg.has_section("api_tokens")


def test_pin_is_idempotent_and_keeps_an_existing_upstream_token():
    cfg = _cfg("[DEFAULT]\nuser_api_token = old\n\n[api_tokens]\nKingdom = new\nKingdom Login Proxy = old\n")

    assert config.pin_kingdom_backend(cfg) is True
    assert cfg.get("api_tokens", "Kingdom") == "new"
    assert config.pin_kingdom_backend(cfg) is False


def test_custom_backend_is_left_alone():
    text = (
        "[DEFAULT]\nsso_api_name = My Guild\nsso_api = https://guild.example.com\n\n"
        "[sso_backends]\nMy Guild = https://guild.example.com\n"
    )
    cfg = _cfg(text)

    assert config.pin_kingdom_backend(cfg) is False
    assert cfg.get("DEFAULT", "sso_api_name") == "My Guild"
    assert not cfg.has_section("api_tokens")


def test_updater_points_at_upstream_under_upstream_exe_name():
    assert "eq-p99-tools/p99-login-proxy" in updater.GITHUB_API_RELEASES_URL.format(max_releases=10)
    assert "TheGrandPackard" not in updater.GITHUB_API_RELEASES_URL
    assert updater.STABLE_EXE_NAME == "P99LoginProxy.exe"


def test_zip_asset_is_chosen_by_name_not_content_type():
    # Asset list of upstream v2.0.1-rc.3, in API order.
    assets = [
        {"name": "P99LoginProxy-2.0.1-rc.3-x86_64.AppImage", "content_type": "application/vnd.appimage"},
        {"name": "P99LoginProxy-2.0.1-rc.5-x86_64.AppImage", "content_type": "application/octet-stream"},
        {"name": "P99LoginProxy-2.0.1-rc.3.zip", "content_type": "application/zip"},
        {"name": "SHA256SUMS", "content_type": "text/plain"},
    ]
    for asset in assets:
        asset["browser_download_url"] = f"https://example.invalid/{asset['name']}"

    assert updater.select_zip_asset_url(assets) == "https://example.invalid/P99LoginProxy-2.0.1-rc.3.zip"
    assert updater.select_zip_asset_url(assets[:2]) is None


def test_upstream_prerelease_is_offered_to_a_stable_fork_build(monkeypatch):
    releases = [
        {"version": semver.Version.parse("2.0.1-rc.3"), "body": "", "prerelease": True, "assets_url": "u"},
        {"version": semver.Version.parse("1.4.5"), "body": "", "prerelease": False, "assets_url": "u"},
    ]
    offered = []
    monkeypatch.setattr(config, "APP_VERSION", semver.Version.parse("1.6.5"))
    monkeypatch.setattr(config, "OPT_INTO_PRERELEASES", False)
    monkeypatch.setattr(updater, "_prompt_and_apply_update", lambda _releases, version: offered.append(version))
    monkeypatch.setattr(updater.QApplication, "activeWindow", staticmethod(lambda: None))

    updater.on_releases_fetched_main_thread(releases, False)

    assert offered == [semver.Version.parse("2.0.1-rc.3")]


def test_old_shortcut_forwards_to_installed_upstream_build(tmp_path):
    kingdom = tmp_path / "KingdomLoginProxy.exe"
    kingdom.write_bytes(b"")

    assert updater.upstream_exe_to_forward(str(kingdom), [str(kingdom)]) is None

    upstream = tmp_path / "P99LoginProxy.exe"
    upstream.write_bytes(b"")

    assert updater.upstream_exe_to_forward(str(kingdom), [str(kingdom)]) == str(upstream)
    assert updater.upstream_exe_to_forward(str(tmp_path / "KingdomLoginProxy-1.6.5.exe"), []) == str(upstream)
    assert updater.upstream_exe_to_forward(str(kingdom), [str(kingdom), "--no-forward"]) is None
    # Never forwards from the upstream exe itself or from a source checkout.
    assert updater.upstream_exe_to_forward(str(upstream), []) is None
    assert updater.upstream_exe_to_forward(str(tmp_path / "python.exe"), []) is None
