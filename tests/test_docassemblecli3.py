import importlib
import io
import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
import requests
import yaml
from click.testing import CliRunner

import docassemblecli3
import docassemblecli3.docassemblecli3 as mod


class DummyResponse:
    def __init__(self, status_code=200, text="", json_data=None, contains=None, raise_error=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self._contains = contains or []
        self._raise_error = raise_error

    def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data

    def raise_for_status(self):
        if self._raise_error is not None:
            raise self._raise_error
        if self.status_code >= 400:
            raise requests.HTTPError(self.text or str(self.status_code))

    def __contains__(self, item):
        return item in self._contains


@pytest.fixture(autouse=True)
def reset_globals(monkeypatch):
    monkeypatch.setattr(mod, "BELL", "\a")
    monkeypatch.setattr(mod, "DEBUG", False)
    monkeypatch.setattr(mod, "FILE_CHECKSUMS", {})
    monkeypatch.setattr(mod, "GITMATCH_COMPILED", None)
    monkeypatch.setattr(mod, "LAST_MODIFIED", {"time": 0, "files": {}, "restart": False})
    mod.CONTEXT_SETTINGS["color"] = None


@pytest.fixture
def runner():
    return CliRunner()


def make_package(directory: Path, setup_text: str, extra_files: dict[str, str] | None = None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "setup.py").write_text(setup_text, encoding="utf-8")
    (directory / "README.md").write_text("readme", encoding="utf-8")
    (directory / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    for relative_path, content in (extra_files or {}).items():
        file_path = directory / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")


def test_package_exports_and_main(monkeypatch):
    called = []

    def fake_cli():
        called.append(True)

    monkeypatch.setattr(docassemblecli3, "cli", fake_cli)
    assert docassemblecli3.__all__ == ["cli"]

    runpy.run_module("docassemblecli3.__main__", run_name="__main__")

    assert called == [True]


def test_cli_callback_and_api_url_type():
    mod.cli.callback(bell=False, color=False, debug=True)

    assert mod.BELL == ""
    assert mod.DEBUG is True
    assert mod.CONTEXT_SETTINGS["color"] is False
    assert mod.config.callback() is None
    assert mod.APIURLType().convert("https://example.com/path", None, None) == "https://example.com"

    with pytest.raises(click.BadParameter):
        mod.APIURLType().convert("not a url", None, None)


def test_validate_package_directory_and_config(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")

    assert mod.validate_package_directory(None, None, str(package_dir)) == str(package_dir.resolve())

    with pytest.raises(click.BadParameter):
        mod.validate_package_directory(None, None, str(tmp_path / "missing"))

    invalid_dir = tmp_path / "invalid"
    invalid_dir.mkdir()
    with pytest.raises(click.BadParameter):
        mod.validate_package_directory(None, None, str(invalid_dir))

    assert mod.validate_and_load_or_create_config(None, None, "") == (None, [])

    default_config = tmp_path / "docassemblecli.yml"
    monkeypatch.setattr(mod, "DEFAULT_CONFIG", str(default_config))
    cfg_path, env = mod.validate_and_load_or_create_config(None, None, str(default_config))
    assert cfg_path == str(default_config.resolve())
    assert env == []
    assert yaml.safe_load(default_config.read_text(encoding="utf-8")) == []

    invalid_config = tmp_path / "bad.yml"
    invalid_config.write_text("key: value\n", encoding="utf-8")
    with pytest.raises(click.BadParameter):
        mod.validate_and_load_or_create_config(None, None, str(invalid_config))

    with pytest.raises(click.BadParameter):
        mod.validate_and_load_or_create_config(None, None, str(tmp_path / "missing.yml"))


def test_display_select_and_env_helpers(monkeypatch, capsys):
    assert mod.name_from_url("") == ""
    assert mod.name_from_url("https://example.com/path") == "example.com"
    assert mod.display_servers([]) == ["No servers found."]

    env = mod.add_or_update_env(apiurl="https://example.com", apikey="key", directory="pkg", playground="demo")
    assert env == [
        {
            "apiurl": "https://example.com",
            "apikey": "key",
            "name": "example.com",
            "directory": "pkg",
            "playground": "demo",
        }
    ]
    assert mod.display_servers(env) == [
        "example.com (default)",
        "  playground: demo",
        "  directory: pkg",
    ]

    updated = mod.add_or_update_env(env=env, apiurl="https://example.com", apikey="new", directory="pkg2")
    assert updated[0]["apikey"] == "new"
    assert updated[0]["directory"] == "pkg2"
    assert 'Server "example.com" was found and updated.' in capsys.readouterr().out

    with pytest.raises(click.BadParameter):
        mod.select_server(cfg=None, env=[], server="named")

    assert mod.select_server(cfg="cfg", env=updated, server="example.com") == updated[0]

    with pytest.raises(click.BadParameter):
        mod.select_server(cfg="cfg", env=updated, server="missing")

    assert mod.select_server(cfg="cfg", env=updated, directory="pkg2") == updated[0]
    assert mod.select_server(cfg="cfg", env=updated) == updated[0]

    monkeypatch.setenv("DOCASSEMBLEAPIURL", "https://env.example.com")
    monkeypatch.setenv("DOCASSEMBLEAPIKEY", "env-key")
    selected = mod.select_server(cfg=None, env=None)
    assert selected["name"] == "env.example.com"

    monkeypatch.delenv("DOCASSEMBLEAPIURL")
    monkeypatch.delenv("DOCASSEMBLEAPIKEY")
    monkeypatch.setattr(mod, "add_server_to_env", lambda cfg, env: [{"name": "prompted"}])
    assert mod.select_server(cfg=None, env=None) == {"name": "prompted"}

    monkeypatch.setattr(mod, "add_server_to_env", lambda **kwargs: [{"name": "api"}])
    assert mod.select_server(cfg="cfg", env=[], apiurl="https://api.example.com", apikey="key") == {"name": "api"}


def test_save_config_and_prompt_for_api(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yml"
    env = [{"name": "example.com"}]

    assert mod.save_config(str(cfg), env) is True
    assert yaml.safe_load(cfg.read_text(encoding="utf-8")) == env

    monkeypatch.setattr(mod.yaml, "dump", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    assert mod.save_config(str(cfg), env) is False

    prompts = iter(["https://prompt.example.com", "  secret  "])
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: next(prompts))
    assert mod.prompt_for_api() == ("https://prompt.example.com", "secret")

    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: False)
    with pytest.raises(click.Abort):
        mod.prompt_for_api(retry=True)


def test_test_apiurl_apikey_and_add_server_to_env(monkeypatch):
    responses = iter(
        [
            DummyResponse(status_code=200),
            DummyResponse(status_code=403, text="forbidden"),
            DummyResponse(status_code=500, text="server error"),
        ]
    )
    monkeypatch.setattr(mod.requests, "get", lambda *args, **kwargs: next(responses))

    assert mod.test_apiurl_apikey("https://example.com", "key") is True
    assert mod.test_apiurl_apikey("https://example.com", "key") is False
    assert mod.test_apiurl_apikey("https://example.com", "key") is False

    def raise_error(*args, **kwargs):
        raise requests.RequestException("network")

    monkeypatch.setattr(mod.requests, "get", raise_error)
    assert mod.test_apiurl_apikey("https://example.com", "key") is False

    prompted = iter(
        [
            ("https://one.example.com", "bad"),
            ("https://two.example.com", "good"),
        ]
    )
    attempts = iter([False, True])
    saved = []
    monkeypatch.setattr(mod, "prompt_for_api", lambda **kwargs: next(prompted))
    monkeypatch.setattr(mod, "test_apiurl_apikey", lambda **kwargs: next(attempts))
    monkeypatch.setattr(mod, "save_config", lambda cfg, env: saved.append((cfg, list(env))) or True)

    env = mod.add_server_to_env(cfg="config.yml", env=[], directory="pkg", playground="play")

    assert env[0]["name"] == "two.example.com"
    assert saved and saved[0][0] == "config.yml"


def test_wait_for_server_success_and_errors(monkeypatch):
    current_time = {"value": 0}

    def fake_time():
        current_time["value"] += 5
        return current_time["value"]

    monkeypatch.setattr(mod.time, "time", fake_time)
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(mod, "DEBUG", True)
    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *args, **kwargs: DummyResponse(status_code=200, json_data={"status": "completed", "ok": True}),
    )

    assert mod.wait_for_server(False, "task-1", "key", "https://example.com", "1.0.0") is True

    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *args, **kwargs: DummyResponse(
            status_code=200,
            json_data={"status": "completed", "ok": False, "error_message": "bad install"},
        ),
    )
    assert mod.wait_for_server(False, "task-2", "key", "https://example.com", "norestart") is False

    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *args, **kwargs: DummyResponse(status_code=500, text="broken", json_data={"status": "running"}),
    )
    assert mod.wait_for_server(True, "task-3", "key", "https://example.com", "1.5.3") == (
        "package_update_status returned 500: broken"
    )

    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.exceptions.RequestException("timeout")),
    )
    with pytest.raises(UnboundLocalError):
        mod.wait_for_server(False, "task-4", "key", "https://example.com", "1.5.3")


def test_package_installer_restart_no_and_dependency_checks(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    make_package(
        package_dir,
        'from setuptools import setup\nsetup(name="docassemble.test", install_requires=["dep_eq==1.0", "dep_le<=2.0", "dep_ge>=3.0", "dep_lt<4.0", "dep_gt>5.0", "plaindep"])\n',
        {"docassemble/test/data/questions/interview.yml": "---\n"},
    )

    class Result:
        stdout = "ignored.txt\n"
        stderr = ""

        def check_returncode(self):
            return None

    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: Result())

    get_calls = []

    def fake_get(url, *args, **kwargs):
        get_calls.append(url)
        return DummyResponse(
            status_code=200,
            json_data=[
                {"name": "dep_eq", "version": "1.0"},
                {"name": "dep_le", "version": "2.0"},
                {"name": "dep_ge", "version": "3.0"},
                {"name": "dep_lt", "version": "3.9"},
                {"name": "dep_gt", "version": "5.1"},
                {"name": "plaindep", "version": "9.0"},
                {"name": "docassemble.test", "version": "0.0.1"},
            ],
        )

    posts = []

    def fake_post(url, data=None, files=None, headers=None, timeout=None):
        file_keys = set(files.keys()) if files else set()
        posts.append((url, data, file_keys))
        if url.endswith("/api/package"):
            if file_keys == {"zip"}:
                return DummyResponse(status_code=200, json_data={"task_id": "task-1"})
            return DummyResponse(status_code=204)
        if url.endswith("/api/clear_cache"):
            return DummyResponse(status_code=204)
        raise AssertionError(url)

    monkeypatch.setattr(mod.requests, "get", fake_get)
    monkeypatch.setattr(mod.requests, "post", fake_post)
    monkeypatch.setattr(mod, "wait_for_server", lambda **kwargs: True)

    result = mod.package_installer(str(package_dir), "https://example.com", "key", playground=None, restart="auto")

    assert result == 0
    assert get_calls == ["https://example.com/api/package"]
    assert posts[0][0] == "https://example.com/api/package"
    assert posts[0][2] == {"zip"}
    assert posts[1][0] == "https://example.com/api/clear_cache"

    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("git")))
    monkeypatch.setattr(mod.requests, "post", lambda *args, **kwargs: DummyResponse(status_code=500, text="bad"))
    assert mod.package_installer(str(package_dir), "https://example.com", "key", playground=None, restart="no") == (
        "package POST returned 500: bad"
    )


def test_package_installer_playground_and_restart_paths(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    make_package(
        package_dir,
        'from setuptools import setup\nsetup(name="docassemble.test", install_requires=[])\n',
        {
            "docassemble/test/module.py": "value = 1\n",
            "docassemble/test/data/questions/interview.yml": "---\n",
        },
    )

    class Result:
        stdout = ""
        stderr = ""

        def check_returncode(self):
            return None

    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: Result())

    get_responses = iter(
        [
            DummyResponse(status_code=200, json_data=[{"name": "docassemble.base", "version": "1.5.3"}]),
            DummyResponse(status_code=200, contains=[]),
        ]
    )
    monkeypatch.setattr(mod.requests, "get", lambda *args, **kwargs: next(get_responses))

    post_responses = iter(
        [
            DummyResponse(status_code=200),
            DummyResponse(status_code=400, text="bad project", json_data="Invalid project."),
            DummyResponse(status_code=204),
            DummyResponse(status_code=200, json_data={"task_id": "task-2"}),
        ]
    )
    monkeypatch.setattr(mod.requests, "post", lambda *args, **kwargs: next(post_responses))
    monkeypatch.setattr(mod, "wait_for_server", lambda **kwargs: False)

    assert mod.package_installer(str(package_dir), "https://example.com", "key", playground="demo", restart="auto") == 1

    monkeypatch.setattr(mod.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(click.ClickException):
        mod.package_installer(str(package_dir), "https://example.com", "key", playground=None, restart="auto")


def test_install_command_and_checksums(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    target = package_dir / "file.txt"
    target.write_text("hello", encoding="utf-8")

    selected_server = {"name": "example.com", "apiurl": "https://example.com", "apikey": "key"}
    calls = []
    monkeypatch.setattr(mod, "select_server", lambda *args, **kwargs: selected_server)
    monkeypatch.setattr(mod, "package_installer", lambda **kwargs: calls.append(kwargs) or 0)

    assert mod.install.callback(str(package_dir), ("cfg", []), (None, None), "", None, "auto") == 0
    assert calls[0]["directory"] == str(package_dir)
    assert mod.calculate_checksum(str(target))

    monkeypatch.setattr(mod, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")), raising=False)
    assert mod.calculate_checksum(str(target)) == ""


def test_scan_directory_matches_ignore_patterns_and_watch_handler(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    kept_file = package_dir / "keep.txt"
    ignored_file = package_dir / "ignored.txt"
    kept_file.write_text("keep", encoding="utf-8")
    ignored_file.write_text("ignore", encoding="utf-8")

    assert bool(mod.matches_ignore_patterns(str(ignored_file), str(package_dir))) is True
    assert bool(mod.matches_ignore_patterns(str(kept_file), str(package_dir))) is False

    mod.GITMATCH_COMPILED = None
    no_gitignore_dir = tmp_path / "nogitignore"
    no_gitignore_dir.mkdir()
    default_ignored = no_gitignore_dir / "build" / "artifact.txt"
    default_ignored.parent.mkdir()
    default_ignored.write_text("artifact", encoding="utf-8")
    assert bool(mod.matches_ignore_patterns(str(default_ignored), str(no_gitignore_dir))) is True

    monkeypatch.setattr(mod, "calculate_checksum", lambda path: f"checksum:{os.path.basename(path)}")
    mod.GITMATCH_COMPILED = None
    mod.DEBUG = True
    mod.scan_directory(str(package_dir))
    assert mod.FILE_CHECKSUMS[str(kept_file)] == "checksum:keep.txt"
    assert str(ignored_file) not in mod.FILE_CHECKSUMS

    handler = mod.WatchHandler(directory=str(package_dir))
    assert handler.on_any_event(SimpleNamespace(is_directory=True)) is None

    monkeypatch.setattr(mod, "matches_ignore_patterns", lambda **kwargs: False)
    monkeypatch.setattr(mod.time, "time", lambda: 123)
    event = SimpleNamespace(is_directory=False, event_type="created", src_path=str(package_dir / "module.py"))
    handler.on_any_event(event)
    assert mod.LAST_MODIFIED == {"time": 123, "files": {event.src_path: True}, "restart": True}

    mod.FILE_CHECKSUMS[event.src_path] = "checksum:module.py"
    mod.LAST_MODIFIED = {"time": 0, "files": {}, "restart": False}
    handler.on_any_event(SimpleNamespace(is_directory=False, event_type="modified", src_path=event.src_path))
    assert mod.LAST_MODIFIED == {"time": 0, "files": {}, "restart": False}


def test_watch_command(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    installs = []

    class FakeObserver:
        def __init__(self):
            self.stopped = False
            self.joined = False

        def schedule(self, *args, **kwargs):
            return None

        def start(self):
            return None

        def stop(self):
            self.stopped = True

        def join(self):
            self.joined = True

    observer = FakeObserver()
    monkeypatch.setattr(mod, "Observer", lambda: observer)
    monkeypatch.setattr(mod, "scan_directory", lambda directory: None)
    monkeypatch.setattr(
        mod,
        "select_server",
        lambda *args, **kwargs: {
            "name": "example.com",
            "apiurl": "https://example.com",
            "apikey": "key",
            "directory": str(package_dir),
            "playground": "stored-playground",
            "startup": "install",
        },
    )
    monkeypatch.setattr(mod, "package_installer", lambda **kwargs: installs.append(kwargs) or 0)
    mod.LAST_MODIFIED = {"time": 1, "files": {str(package_dir / "file.yml"): True}, "restart": True}

    sleep_calls = {"count": 0}

    def fake_sleep(seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] > 1:
            raise KeyboardInterrupt("stop")

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)

    result = mod.watch.callback(str(package_dir), ("cfg", []), (None, None), "", None, "auto", 0)

    assert result == '\nStopping "docassemblecli3 watch".'
    assert observer.stopped is True
    assert observer.joined is True
    assert installs[0]["playground"] == "stored-playground"
    assert installs[1]["restart"] == "yes"


def test_create_command(tmp_path, monkeypatch):
    prompts = iter(["", "", "", "", "Custom", "1.2.3"])
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: next(prompts))

    output_dir = tmp_path / "docassemble-demo"
    assert mod.create.callback("demo", None, None, None, None, None, None, str(output_dir)) == 0
    assert (output_dir / "setup.py").is_file()
    assert (output_dir / "docassemble" / "demo" / "__init__.py").read_text(encoding="utf-8").strip() == (
        "__version__ = '1.2.3'"
    )

    existing_file = tmp_path / "taken"
    existing_file.write_text("x", encoding="utf-8")
    assert mod.create.callback(
        "demo", "Name", "dev@example.com", "Desc", "https://example.com", "MIT", "0.1.0", str(existing_file)
    ) == (f"Cannot create the directory {existing_file} because the path already exists.")

    existing_dir = tmp_path / "existing"
    existing_dir.mkdir()
    (existing_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")
    assert mod.create.callback(
        "demo", "Name", "dev@example.com", "Desc", "https://example.com", "Apache-2.0", "0.1.0", str(existing_dir)
    ) == (f"The directory {existing_dir} already has a package in it.")

    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: " ")
    assert (
        mod.create.callback(None, None, None, None, None, None, None, None)
        == "The package name you entered is invalid."
    )


def test_config_commands(tmp_path, monkeypatch, runner):
    config_path = tmp_path / "config.yml"
    config_path.write_text("[]\n", encoding="utf-8")

    added = []
    monkeypatch.setattr(mod, "add_server_to_env", lambda **kwargs: added.append(kwargs) or kwargs["env"])
    (tmp_path / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")
    result = runner.invoke(
        mod.cli,
        [
            "config",
            "add",
            "--config",
            str(config_path),
            "--api",
            "https://example.com",
            "key",
            "--directory",
            str(tmp_path),
            "--playground",
            "demo",
        ],
    )
    assert result.exit_code == 0
    assert added[0]["directory"] == str(tmp_path.resolve())

    config = (str(config_path), [{"name": "example.com", "apiurl": "https://example.com", "apikey": "key"}])
    monkeypatch.setattr(mod, "save_config", lambda **kwargs: True)
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: "example.com")
    assert mod.remove.callback(config, None) is None

    mod.display.callback((str(config_path), [{"name": "example.com"}]))

    new_path = tmp_path / "new-config.yml"
    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(mod, "prompt_for_api", lambda: ("https://example.com", "key"))
    new_file = new_path.open("w", encoding="utf-8")
    try:
        assert mod.new.callback(new_file) is None
    finally:
        new_file.close()

    nonempty = tmp_path / "nonempty.yml"
    nonempty.write_text("value", encoding="utf-8")
    fake_file = io.StringIO()
    fake_file.name = str(nonempty)
    with pytest.raises(click.BadParameter):
        mod.new.callback(fake_file)

    response = DummyResponse(status_code=403, text="forbidden")
    monkeypatch.setattr(mod.requests, "get", lambda *args, **kwargs: response)
    with pytest.raises(click.ClickException):
        mod.server_version.callback(
            (str(config_path), [{"name": "example.com", "apiurl": "https://example.com", "apikey": "key"}]),
            (None, None),
            "",
        )

    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *args, **kwargs: DummyResponse(
            status_code=200,
            json_data=[{"name": "docassemble.base", "version": "1.6.0"}],
        ),
    )
    mod.server_version.callback(
        (str(config_path), [{"name": "example.com", "apiurl": "https://example.com", "apikey": "key"}]),
        (None, None),
        "",
    )

    tested = []
    monkeypatch.setattr(mod, "test_apiurl_apikey", lambda **kwargs: tested.append(kwargs) or True)
    mod.test.callback(
        (str(config_path), [{"name": "example.com", "apiurl": "https://example.com", "apikey": "key"}]),
        (None, None),
        "",
    )
    assert tested == [{"apiurl": "https://example.com", "apikey": "key"}]


def test_display_servers_install_playground_and_create_defaults(tmp_path, monkeypatch):
    servers = mod.display_servers(
        [
            {"name": "first", "startup": "install"},
            {"name": "second", "playground": "demo", "directory": "/pkg"},
        ]
    )
    assert servers == [
        "first (default)",
        "  startup: install",
        "second",
        "  playground: demo",
        "  directory: /pkg",
    ]

    env = [{"name": "example.com", "apiurl": "https://example.com", "apikey": "key"}]
    updated = mod.add_or_update_env(env=env, apiurl="https://example.com", apikey="key", playground="demo")
    assert updated[0]["playground"] == "demo"

    installs = []
    monkeypatch.setattr(
        mod,
        "select_server",
        lambda *args, **kwargs: {"name": "example.com", "apiurl": "https://example.com", "apikey": "key"},
    )
    monkeypatch.setattr(mod, "package_installer", lambda **kwargs: installs.append(kwargs) or 0)
    assert mod.install.callback(str(tmp_path), ("cfg", []), (None, None), "", "demo", "auto") == 0
    assert installs[0]["playground"] == "demo"

    prompts = iter(["", "", "", "", "MIT", "0.0.1"])
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: next(prompts))
    monkeypatch.chdir(tmp_path)
    assert mod.create.callback("docassemble.demo", None, None, None, None, None, None, None) == 0
    created = tmp_path / "docassemble-demo"
    assert created.is_dir()
    assert "The MIT License (MIT)" in (created / "LICENSE").read_text(encoding="utf-8")


def test_wait_for_server_loop_and_nonplayground_info(monkeypatch):
    current_time = {"value": 0}

    def fake_time():
        current_time["value"] += 1
        return current_time["value"]

    monkeypatch.setattr(mod.time, "time", fake_time)
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)

    responses = iter(
        [
            DummyResponse(status_code=200, json_data={"status": "running"}),
            DummyResponse(status_code=200, json_data={"status": "completed"}),
        ]
    )
    monkeypatch.setattr(mod.requests, "get", lambda *args, **kwargs: next(responses))
    assert mod.wait_for_server(True, "task-loop", "key", "https://example.com", "1.5.3") is True

    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *args, **kwargs: DummyResponse(status_code=200, json_data={"status": "unknown", "ok": False}),
    )
    assert mod.wait_for_server(False, "task-unknown", "key", "https://example.com", "norestart") is False


def test_package_installer_dependency_parsing_and_debug(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    make_package(
        package_dir,
        'from setuptools import setup\nsetup(name="ignored")\n',
        {"docassemble/test/data/questions/interview.yml": "---\n"},
    )

    real_search = mod.re.search

    class FakeGroupText:
        def split(self, _separator):
            return [
                ",dep_eq==1.0,",
                ",dep_le<=2.0,",
                ",dep_ge>=3.0,",
                ",dep_lt<4.0,",
                ",dep_gt>5.0,",
                ",plain,",
            ]

    class FakeMatch:
        def __init__(self, groups):
            self._groups = groups

        def group(self, index):
            return self._groups[index - 1]

    def fake_search(pattern, text, flags=0):
        if "\\bname=" in pattern:
            return FakeMatch(('"', "docassemble.test", '"'))
        if "install_requires" in pattern:
            return FakeMatch((FakeGroupText(),))
        if pattern == r"""(.*)(<=|>=|==|<|>)(.*)""":
            return real_search(pattern, text, flags=flags)
        return real_search(pattern, text, flags=flags)

    class Result:
        stdout = ""
        stderr = ""

        def check_returncode(self):
            return None

    monkeypatch.setattr(mod.re, "search", fake_search)
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(mod, "DEBUG", True)
    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *args, **kwargs: DummyResponse(
            status_code=200,
            json_data=[
                {"name": "dep_eq", "version": "1.0"},
                {"name": "dep_le", "version": "2.0"},
                {"name": "dep_ge", "version": "3.0"},
                {"name": "dep_lt", "version": "3.9"},
                {"name": "dep_gt", "version": "5.1"},
                {"name": "plain", "version": "1.0"},
                {"name": "docassemble.test", "version": "0.0.1"},
            ],
        ),
    )
    posts = iter([DummyResponse(status_code=200, json_data={"task_id": "task"}), DummyResponse(status_code=204)])
    monkeypatch.setattr(mod.requests, "post", lambda *args, **kwargs: next(posts))
    monkeypatch.setattr(mod, "wait_for_server", lambda **kwargs: True)

    assert mod.package_installer(str(package_dir), "https://example.com", "key", playground=None, restart="auto") == 0


def test_package_installer_nonplayground_error_paths(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    make_package(
        package_dir,
        'from setuptools import setup\nsetup(name="docassemble.test", install_requires=[])\n',
        {"docassemble/test/data/questions/interview.yml": "---\n"},
    )

    class Result:
        stdout = ""
        stderr = ""

        def check_returncode(self):
            return None

    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: Result())

    monkeypatch.setattr(mod.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(click.ClickException):
        mod.package_installer(str(package_dir), "https://example.com", "key", playground=None, restart="auto")

    monkeypatch.setattr(mod.requests, "get", lambda *args, **kwargs: DummyResponse(status_code=500, text="bad"))
    assert mod.package_installer(str(package_dir), "https://example.com", "key", playground=None, restart="auto") == (
        "/api/package returned 500: bad"
    )

    bare_dir = tmp_path / "bare"
    make_package(
        bare_dir, "from setuptools import setup\nsetup()\n", {"docassemble/test/data/questions/interview.yml": "---\n"}
    )
    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *args, **kwargs: DummyResponse(
            status_code=200,
            json_data=[{"name": "docassemble.base", "version": "1.5.3"}],
            raise_error=requests.HTTPError("forbidden"),
        ),
    )
    monkeypatch.setattr(
        mod.requests, "post", lambda *args, **kwargs: DummyResponse(status_code=200, json_data={"task_id": "task"})
    )
    monkeypatch.setattr(mod, "wait_for_server", lambda **kwargs: True)
    assert mod.package_installer(str(bare_dir), "https://example.com", "key", playground=None, restart="auto") == 0

    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *args, **kwargs: DummyResponse(
            status_code=403, text="forbidden", raise_error=requests.HTTPError("forbidden")
        ),
    )
    with pytest.raises(click.ClickException):
        mod.package_installer(str(package_dir), "https://example.com", "key", playground=None, restart="yes")

    monkeypatch.setattr(mod.requests, "get", lambda *args, **kwargs: DummyResponse(status_code=200, json_data=[]))
    monkeypatch.setattr(mod.requests, "post", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("post")))
    with pytest.raises(click.ClickException):
        mod.package_installer(str(package_dir), "https://example.com", "key", playground=None, restart="yes")

    monkeypatch.setattr(
        mod.requests,
        "post",
        lambda *args, **kwargs: (
            DummyResponse(status_code=200, json_data={"task_id": "task"})
            if kwargs.get("files")
            else (_ for _ in ()).throw(RuntimeError("cache"))
        ),
    )
    monkeypatch.setattr(mod, "wait_for_server", lambda **kwargs: True)
    with pytest.raises(click.ClickException):
        mod.package_installer(str(package_dir), "https://example.com", "key", playground=None, restart="no")

    monkeypatch.setattr(
        mod.requests,
        "post",
        lambda *args, **kwargs: (
            DummyResponse(status_code=200, json_data={"task_id": "task"})
            if kwargs.get("files")
            else DummyResponse(status_code=500, text="cache bad")
        ),
    )
    assert mod.package_installer(str(package_dir), "https://example.com", "key", playground=None, restart="no") == (
        "clear_cache returned 500: cache bad"
    )


def test_package_installer_playground_error_paths(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    make_package(
        package_dir,
        'from setuptools import setup\nsetup(name="docassemble.test", install_requires=[])\n',
        {"docassemble/test/module.py": "value = 1\n"},
    )

    class Result:
        stdout = ""
        stderr = ""

        def check_returncode(self):
            return None

    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(mod, "wait_for_server", lambda **kwargs: True)

    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda url, *args, **kwargs: (
            DummyResponse(status_code=200, json_data=[{"name": "docassemble.base", "version": "1.5.3"}])
            if url.endswith("/api/package")
            else DummyResponse(status_code=200, contains=[])
        ),
    )
    monkeypatch.setattr(
        mod.requests,
        "post",
        lambda url, *args, **kwargs: (
            (_ for _ in ()).throw(RuntimeError("create project"))
            if url.endswith("/api/playground/project")
            else DummyResponse(status_code=200, json_data={"task_id": "task"})
        ),
    )
    assert mod.package_installer(str(package_dir), "https://example.com", "key", playground="demo", restart="auto") == (
        "create project POST returned "
    )

    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda url, *args, **kwargs: (
            DummyResponse(status_code=200, json_data=[{"name": "docassemble.base", "version": "1.5.3"}])
            if url.endswith("/api/package")
            else DummyResponse(status_code=500, text="missing")
        ),
    )
    assert mod.package_installer(str(package_dir), "https://example.com", "key", playground="demo", restart="auto") == (
        "playground list of projects GET returned 500: missing"
    )

    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda url, *args, **kwargs: (
            DummyResponse(status_code=200, json_data=[{"name": "docassemble.base", "version": "1.5.3"}])
            if url.endswith("/api/package")
            else DummyResponse(status_code=200, contains=["demo"])
        ),
    )
    monkeypatch.setattr(
        mod.requests, "post", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("install post"))
    )
    with pytest.raises(click.ClickException):
        mod.package_installer(str(package_dir), "https://example.com", "key", playground="demo", restart="auto")

    post_sequence = iter([DummyResponse(status_code=400, text="bad", json_data=ValueError("json"))])
    monkeypatch.setattr(mod.requests, "post", lambda *args, **kwargs: next(post_sequence))
    assert mod.package_installer(str(package_dir), "https://example.com", "key", playground="demo", restart="auto") == (
        "playground_install POST returned 400: bad"
    )

    call_count = {"count": 0}

    def post_raise_second(*args, **kwargs):
        call_count["count"] += 1
        if call_count["count"] == 1:
            return DummyResponse(status_code=400, text="bad", json_data="Invalid project.")
        raise RuntimeError("project create")

    monkeypatch.setattr(mod.requests, "post", post_raise_second)
    with pytest.raises(click.ClickException):
        mod.package_installer(str(package_dir), "https://example.com", "key", playground="demo", restart="auto")

    post_sequence = iter(
        [
            DummyResponse(status_code=400, text="bad", json_data="Invalid project."),
            DummyResponse(status_code=500, text="project bad"),
        ]
    )
    monkeypatch.setattr(mod.requests, "post", lambda *args, **kwargs: next(post_sequence))
    assert mod.package_installer(str(package_dir), "https://example.com", "key", playground="demo", restart="auto") == (
        "needed to create playground project but POST to api/playground/project returned 500: project bad"
    )

    call_count = {"count": 0}

    def post_raise_third(*args, **kwargs):
        call_count["count"] += 1
        if call_count["count"] == 1:
            return DummyResponse(status_code=400, text="bad", json_data="Invalid project.")
        if call_count["count"] == 2:
            return DummyResponse(status_code=204)
        raise RuntimeError("second install")

    monkeypatch.setattr(mod.requests, "post", post_raise_third)
    with pytest.raises(click.ClickException):
        mod.package_installer(str(package_dir), "https://example.com", "key", playground="demo", restart="auto")

    post_sequence = iter([DummyResponse(status_code=200, text="not-json", json_data=ValueError("json"))])
    monkeypatch.setattr(mod.requests, "post", lambda *args, **kwargs: next(post_sequence))
    assert (
        mod.package_installer(str(package_dir), "https://example.com", "key", playground="demo", restart="auto")
        == "not-json"
    )

    monkeypatch.setattr(mod.requests, "post", lambda *args, **kwargs: DummyResponse(status_code=204))
    assert mod.package_installer(str(package_dir), "https://example.com", "key", playground="demo", restart="auto") == 0

    monkeypatch.setattr(
        mod.requests, "post", lambda *args, **kwargs: DummyResponse(status_code=500, text="bad install")
    )
    assert mod.package_installer(str(package_dir), "https://example.com", "key", playground="demo", restart="auto") == (
        "playground_install POST returned 500: bad install"
    )


def test_watch_package_location_and_exception(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()

    class FakeObserver:
        def schedule(self, *args, **kwargs):
            return None

        def start(self):
            return None

        def stop(self):
            return None

        def join(self):
            return None

    monkeypatch.setattr(mod, "Observer", lambda: FakeObserver())
    monkeypatch.setattr(mod, "scan_directory", lambda directory: None)
    monkeypatch.setattr(
        mod,
        "select_server",
        lambda *args, **kwargs: {"name": "example.com", "apiurl": "https://example.com", "apikey": "key"},
    )
    monkeypatch.setattr(mod, "package_installer", lambda **kwargs: 0)
    mod.LAST_MODIFIED = {"time": 1, "files": {str(package_dir / "file.yml"): True}, "restart": False}

    sleep_calls = {"count": 0}

    def fake_sleep(seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] > 1:
            raise RuntimeError("stop")

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)

    assert (
        mod.watch.callback(str(package_dir), ("cfg", []), (None, None), "", None, "auto", 0)
        == '\nStopping "docassemblecli3 watch".'
    )


def test_new_config_failure_and_server_version_debug(tmp_path, monkeypatch):
    unusable = io.StringIO()
    unusable.name = str(tmp_path / "unusable.yml")
    monkeypatch.setattr(mod.yaml, "dump", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("nope")))
    with pytest.raises(click.BadParameter):
        mod.new.callback(unusable)

    monkeypatch.setattr(mod, "DEBUG", True)
    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *args, **kwargs: DummyResponse(
            status_code=200, json_data=[{"name": "docassemble.base", "version": "1.6.0"}]
        ),
    )
    mod.server_version.callback(
        (str(tmp_path / "cfg.yml"), [{"name": "example.com", "apiurl": "https://example.com", "apikey": "key"}]),
        (None, None),
        "",
    )


def test_main_module_import_does_not_run_cli(monkeypatch):
    called = []

    def fake_cli():
        called.append(True)

    monkeypatch.setattr(docassemblecli3, "cli", fake_cli)
    sys.modules.pop("docassemblecli3.__main__", None)
    importlib.import_module("docassemblecli3.__main__")

    assert called == []


def test_remaining_helper_branches(monkeypatch):
    original_add_server_to_env = mod.add_server_to_env
    fallback = []
    monkeypatch.delenv("DOCASSEMBLEAPIURL", raising=False)
    monkeypatch.delenv("DOCASSEMBLEAPIKEY", raising=False)
    monkeypatch.setattr(
        mod, "add_server_to_env", lambda cfg, env: fallback.append((cfg, env)) or [{"name": "fallback"}]
    )
    assert mod.select_server(cfg=None, env=[]) == {"name": "fallback"}

    env = [
        {"name": "first", "directory": "one"},
        {"name": "second", "directory": "two"},
    ]
    assert mod.select_server(cfg="cfg", env=env, directory="missing") == env[0]
    assert mod.select_server(cfg="cfg", env=env, directory="two") == env[1]

    updated = mod.add_or_update_env(
        env=[
            {"name": "other", "apiurl": "https://other.example.com", "apikey": "x"},
            {"name": "example.com", "apiurl": "old", "apikey": "old"},
        ],
        apiurl="https://example.com",
        apikey="new",
    )
    assert updated[1]["apikey"] == "new"

    prompts = iter(["https://retry.example.com", "key"])
    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: next(prompts))
    assert mod.prompt_for_api(retry=True) == ("https://retry.example.com", "key")

    monkeypatch.setattr(mod, "add_server_to_env", original_add_server_to_env)
    monkeypatch.setattr(mod, "prompt_for_api", lambda **kwargs: ("https://prompt.example.com", "key"))
    monkeypatch.setattr(mod, "test_apiurl_apikey", lambda **kwargs: True)
    assert mod.add_server_to_env(cfg=None, env=[], apiurl=None, apikey=None)[0]["name"] == "prompt.example.com"


def test_wait_for_server_exhausts_loop_for_playground(monkeypatch):
    monkeypatch.setattr(mod.time, "time", lambda: 1)
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *args, **kwargs: DummyResponse(status_code=200, json_data={"status": "running", "ok": False}),
    )

    assert mod.wait_for_server(True, "task-loop", "key", "https://example.com", "1.0.0") is False


def test_package_installer_setup_cfg_only_and_nonmatching_dependencies(tmp_path, monkeypatch):
    cfg_only_dir = tmp_path / "cfgonly"
    cfg_only_dir.mkdir()
    (cfg_only_dir / "setup.cfg").write_text("[metadata]\nname = demo\n", encoding="utf-8")
    (cfg_only_dir / "README.md").write_text("readme", encoding="utf-8")
    data_file = cfg_only_dir / "docassemble" / "demo" / "data" / "questions" / "file.yml"
    data_file.parent.mkdir(parents=True)
    data_file.write_text("---\n", encoding="utf-8")

    class Result:
        stdout = ""
        stderr = ""

        def check_returncode(self):
            return None

    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: Result())

    def post_package(url, data=None, files=None, headers=None, timeout=None):
        if files:
            return DummyResponse(status_code=200, json_data={"task_id": "cfg-task"})
        return DummyResponse(status_code=204)

    monkeypatch.setattr(mod.requests, "post", post_package)
    monkeypatch.setattr(mod, "wait_for_server", lambda **kwargs: True)
    assert mod.package_installer(str(cfg_only_dir), "https://example.com", "key", playground=None, restart="no") == 0

    package_dir = tmp_path / "deps"
    make_package(
        package_dir,
        'from setuptools import setup\nsetup(name="ignored")\n',
        {"docassemble/test/module.py": "value = 1\n"},
    )
    real_search = mod.re.search

    class FakeGroupText:
        def split(self, _separator):
            return [",dep_eq==2.0,", ",dep_le<=1.0,", ",dep_ge>=2.0,", ",dep_lt<1.0,", ",dep_gt>2.0,"]

    class FakeMatch:
        def __init__(self, groups):
            self._groups = groups

        def group(self, index):
            return self._groups[index - 1]

    def fake_search(pattern, text, flags=0):
        if "\\bname=" in pattern:
            return FakeMatch(('"', "docassemble.test", '"'))
        if "install_requires" in pattern:
            return FakeMatch((FakeGroupText(),))
        return real_search(pattern, text, flags=flags)

    monkeypatch.setattr(mod.re, "search", fake_search)

    def fake_get(url, *args, **kwargs):
        if url.endswith("/api/playground/project"):
            return DummyResponse(status_code=200, contains=["default"])
        return DummyResponse(
            status_code=200,
            json_data=[
                {"name": "not-base", "version": "0.1"},
                {"name": "dep_eq", "version": "1.0"},
                {"name": "dep_le", "version": "2.0"},
                {"name": "dep_ge", "version": "1.0"},
                {"name": "dep_lt", "version": "2.0"},
                {"name": "dep_gt", "version": "2.0"},
                {"name": "docassemble.base", "version": "1.5.3"},
            ],
        )

    monkeypatch.setattr(mod.requests, "get", fake_get)
    monkeypatch.setattr(mod.requests, "post", lambda *args, **kwargs: DummyResponse(status_code=204))
    assert (
        mod.package_installer(str(package_dir), "https://example.com", "key", playground="default", restart="auto") == 0
    )


def test_package_installer_additional_restart_branches(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    make_package(
        package_dir,
        'from setuptools import setup\nsetup(name="docassemble.test", install_requires=[])\n',
        {"docassemble/test/module.py": "value = 1\n"},
    )

    class Result:
        stdout = ""
        stderr = ""

        def check_returncode(self):
            return None

    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: Result())

    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *args, **kwargs: DummyResponse(status_code=500, text="bad", raise_error=requests.HTTPError("bad")),
    )
    with pytest.raises(click.ClickException):
        mod.package_installer(str(package_dir), "https://example.com", "key", playground=None, restart="yes")

    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *args, **kwargs: DummyResponse(
            status_code=200, json_data=[{"name": "docassemble.base", "version": "1.5.3"}]
        ),
    )
    monkeypatch.setattr(
        mod.requests, "post", lambda *args, **kwargs: DummyResponse(status_code=200, json_data={"task_id": "task"})
    )
    monkeypatch.setattr(mod, "wait_for_server", lambda **kwargs: False)
    assert mod.package_installer(str(package_dir), "https://example.com", "key", playground=None, restart="yes") == 0


def test_watch_handler_and_scan_remaining_branches(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    keep_file = package_dir / "keep.txt"
    keep_file.write_text("keep", encoding="utf-8")

    monkeypatch.setattr(mod, "matches_ignore_patterns", lambda **kwargs: False)
    monkeypatch.setattr(mod, "calculate_checksum", lambda path: "checksum")
    mod.DEBUG = False
    mod.scan_directory(str(package_dir))
    assert mod.FILE_CHECKSUMS[str(keep_file)] == "checksum"

    handler = mod.WatchHandler(directory=str(package_dir))
    monkeypatch.setattr(mod.time, "time", lambda: 7)

    monkeypatch.setattr(mod, "matches_ignore_patterns", lambda **kwargs: True)
    handler.on_any_event(
        SimpleNamespace(is_directory=False, event_type="modified", src_path=str(package_dir / "ignored.txt"))
    )
    assert mod.LAST_MODIFIED == {"time": 0, "files": {}, "restart": False}

    monkeypatch.setattr(mod, "matches_ignore_patterns", lambda **kwargs: False)
    handler.on_any_event(
        SimpleNamespace(is_directory=False, event_type="created", src_path=str(package_dir / "notes.txt"))
    )
    assert mod.LAST_MODIFIED["restart"] is False
    handler.on_any_event(
        SimpleNamespace(is_directory=False, event_type="deleted", src_path=str(package_dir / "gone.txt"))
    )


def test_watch_with_explicit_playground(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()

    class FakeObserver:
        def schedule(self, *args, **kwargs):
            return None

        def start(self):
            return None

        def stop(self):
            return None

        def join(self):
            return None

    monkeypatch.setattr(mod, "Observer", lambda: FakeObserver())
    monkeypatch.setattr(mod, "scan_directory", lambda directory: None)
    monkeypatch.setattr(
        mod,
        "select_server",
        lambda *args, **kwargs: {"name": "example.com", "apiurl": "https://example.com", "apikey": "key"},
    )
    monkeypatch.setattr(mod, "package_installer", lambda **kwargs: 0)
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: (_ for _ in ()).throw(RuntimeError("stop")))
    mod.LAST_MODIFIED = {"time": 0, "files": {}, "restart": False}

    assert (
        mod.watch.callback(str(package_dir), ("cfg", []), (None, None), "", "explicit", "auto", 0)
        == '\nStopping "docassemblecli3 watch".'
    )


def test_create_and_config_remaining_branches(tmp_path, monkeypatch):
    existing_dir = tmp_path / "existing"
    existing_dir.mkdir()
    nested = existing_dir / "docassemble" / "demo" / "data"
    for folder in (nested / "questions", nested / "templates", nested / "static", nested / "sources"):
        folder.mkdir(parents=True, exist_ok=True)

    assert (
        mod.create.callback(
            "demo",
            "Name",
            "dev@example.com",
            "Desc",
            "https://example.com",
            "Apache-2.0",
            "1.0.0",
            str(existing_dir),
        )
        == 0
    )

    prompt_values = iter(["Name", "dev@example.com", "Desc", "https://example.com", "Apache-2.0", "1.0.0"])
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: next(prompt_values))
    assert mod.create.callback("demo2", None, None, None, None, None, None, str(tmp_path / "newdir")) == 0

    config = (str(tmp_path / "cfg.yml"), [{"name": "example.com", "apiurl": "https://example.com", "apikey": "key"}])
    monkeypatch.setattr(mod, "save_config", lambda **kwargs: True)
    assert mod.remove.callback(config, "example.com") is None

    new_file = (tmp_path / "no-add.yml").open("w", encoding="utf-8")
    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: False)
    try:
        assert mod.new.callback(new_file) is None
    finally:
        new_file.close()

    config_for_server = (
        str(tmp_path / "cfg.yml"),
        [{"name": "example.com", "apiurl": "https://example.com", "apikey": "key"}],
    )

    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *args, **kwargs: DummyResponse(
            status_code=200,
            json_data=[{"name": "other", "version": "0.1"}, {"name": "docassemble.base", "version": "1.6.0"}],
        ),
    )
    mod.server_version.callback(config_for_server, (None, None), "")

    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *args, **kwargs: DummyResponse(status_code=500, text="bad", raise_error=requests.HTTPError("bad")),
    )
    with pytest.raises(click.ClickException):
        mod.server_version.callback(config_for_server, (None, None), "")


def test_add_server_to_env_without_prompt_and_save_failure(monkeypatch):
    monkeypatch.setattr(mod, "test_apiurl_apikey", lambda **kwargs: True)
    monkeypatch.setattr(mod, "save_config", lambda cfg, env: False)

    env = mod.add_server_to_env(
        cfg="config.yml",
        env=[],
        apiurl="https://example.com",
        apikey="key",
    )

    assert env[0]["name"] == "example.com"


def test_package_installer_final_dependency_branches(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    make_package(
        package_dir,
        'from setuptools import setup\nsetup(name="ignored")\n',
        {"docassemble/test/module.py": "value = 1\n"},
    )
    real_search = mod.re.search

    class Result:
        stdout = ""
        stderr = ""

        def check_returncode(self):
            return None

    class FakeGroupText:
        def split(self, _separator):
            return [",dep_eq==2.0,", ",dep_plain,", ",dep_weird!=3.0,"]

    class FakeMatch:
        def __init__(self, groups):
            self._groups = groups

        def group(self, index):
            return self._groups[index - 1]

    def fake_search(pattern, text, flags=0):
        if "\\bname=" in pattern:
            return FakeMatch(('"', "docassemble.test", '"'))
        if "install_requires" in pattern:
            return FakeMatch((FakeGroupText(),))
        if pattern == r"""(.*)(<=|>=|==|<|>)(.*)""" and text == "dep_weird!=3.0":
            return FakeMatch(("dep_weird", "!=", "3.0"))
        return real_search(pattern, text, flags=flags)

    def fake_get(url, *args, **kwargs):
        return DummyResponse(
            status_code=200,
            json_data=[
                {"name": "dep_eq", "version": "1.0"},
                {"name": "dep_plain", "version": "1.0"},
                {"name": "docassemble.base", "version": "1.5.3"},
            ],
        )

    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(mod.re, "search", fake_search)
    monkeypatch.setattr(mod.requests, "get", fake_get)
    monkeypatch.setattr(
        mod.requests, "post", lambda *args, **kwargs: DummyResponse(status_code=200, json_data={"task_id": "task"})
    )
    monkeypatch.setattr(mod, "wait_for_server", lambda **kwargs: True)

    assert mod.package_installer(str(package_dir), "https://example.com", "key", playground=None, restart="auto") == 0
