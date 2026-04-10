import importlib
import io
import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import click
import niquests as requests
import pytest
import yaml
from click.testing import CliRunner

import docassemblecli3
import docassemblecli3.docassemblecli3 as mod


class DummyResponse:
    def __init__(self, status_code=200, text="", json_data=None, contains=None, raise_error=None, chunks=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self._contains = contains or []
        self._raise_error = raise_error
        self._chunks = chunks or [b""]

    def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data

    def raise_for_status(self):
        if self._raise_error is not None:
            raise self._raise_error
        if self.status_code >= 400:
            raise requests.HTTPError(self.text or str(self.status_code))

    def iter_content(self, _chunk_size):
        yield from self._chunks

    def __contains__(self, item):
        return item in self._contains


@pytest.fixture(autouse=True)
def reset_globals(monkeypatch):
    monkeypatch.setattr(mod, "BELL", "\a")
    monkeypatch.setattr(mod, "DEBUG", False)
    monkeypatch.setattr(mod, "WATCH_IGNORE_MTIME", None)
    monkeypatch.setattr(mod, "FILE_CHECKSUMS", {})
    monkeypatch.setattr(mod, "FULL_INSTALL_DONE", False)
    monkeypatch.setattr(mod, "GITMATCH_COMPILED", None)
    monkeypatch.setattr(mod, "GITMATCH_DIRECTORY", None)
    monkeypatch.setattr(mod, "GITIGNORE_MTIME", None)
    monkeypatch.setattr(mod, "LAST_MODIFIED", {"time": 0, "files": {}, "restart": False})
    monkeypatch.setattr(mod, "WATCH_SETTLE_DELAY", 0.6)
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


def test_metadata_helpers_and_dependency_parsing(tmp_path):
    assert mod.package_metadata_files_present(str(tmp_path)) is False

    deps = mod.parse_dependency_strings([None, "# comment", " dep>=1.0 ; python_version>='3.12' ", "plain,"])
    assert deps == {
        "dep": {"installed": False, "operator": ">=", "version": "1.0"},
        "plain": {"installed": False, "operator": None, "version": None},
    }

    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / "setup.cfg").write_text(
        "[metadata]\nname = docassemble.cfg\n[options]\ninstall_requires =\n dep_a>=1.0\n plain\n",
        encoding="utf-8",
    )
    assert mod.package_metadata_files_present(str(package_dir)) is True
    package_name, dependencies = mod.load_package_metadata(str(package_dir), ["setup.cfg"])
    assert package_name == "docassemble.cfg"
    assert dependencies["dep_a"]["operator"] == ">="
    assert dependencies["plain"]["operator"] is None
    assert mod.normalize_package_name("demo") == "docassemble.demo"
    assert mod.normalize_package_name("docassemble-demo") == "docassemble.demo"

    pyproject_dir = tmp_path / "pyproject-weird"
    pyproject_dir.mkdir()
    (pyproject_dir / "pyproject.toml").write_text("project = 'oops'\n", encoding="utf-8")
    package_name, dependencies = mod.load_package_metadata(str(pyproject_dir), ["pyproject.toml"])
    assert package_name is None
    assert dependencies == {}

    pyproject_no_name_dir = tmp_path / "pyproject-no-name"
    pyproject_no_name_dir.mkdir()
    (pyproject_no_name_dir / "pyproject.toml").write_text(
        "[project]\ndependencies = ['dep_only>=1.0']\n",
        encoding="utf-8",
    )
    package_name, dependencies = mod.load_package_metadata(str(pyproject_no_name_dir), ["pyproject.toml"])
    assert package_name is None
    assert dependencies["dep_only"]["version"] == "1.0"

    combo_dir = tmp_path / "combo"
    combo_dir.mkdir()
    (combo_dir / "pyproject.toml").write_text("[project]\nname='docassemble.combo'\n", encoding="utf-8")
    (combo_dir / "setup.cfg").write_text("[options]\ninstall_requires =\n dep_b==2.0\n", encoding="utf-8")
    (combo_dir / "setup.py").write_text(
        'from setuptools import setup\nsetup(name="ignored", install_requires=["dep_c>=3.0"])\n',
        encoding="utf-8",
    )
    package_name, dependencies = mod.load_package_metadata(str(combo_dir), ["pyproject.toml", "setup.cfg", "setup.py"])
    assert package_name == "docassemble.combo"
    assert dependencies["dep_b"]["version"] == "2.0"
    assert dependencies["dep_c"]["version"] == "3.0"


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

    setup_cfg_dir = tmp_path / "setup-cfg"
    setup_cfg_dir.mkdir()
    (setup_cfg_dir / "setup.cfg").write_text("[metadata]\nname = docassemble.test\n", encoding="utf-8")
    assert mod.validate_package_directory(None, None, str(setup_cfg_dir)) == str(setup_cfg_dir.resolve())

    pyproject_dir = tmp_path / "pyproject"
    pyproject_dir.mkdir()
    (pyproject_dir / "pyproject.toml").write_text("[project]\nname='docassemble.test'\n", encoding="utf-8")
    assert mod.validate_package_directory(None, None, str(pyproject_dir)) == str(pyproject_dir.resolve())

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


def test_common_params_for_directory_and_playground_decorator(tmp_path, runner):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")

    @click.command()
    @mod.common_params_for_directory_and_playground
    def command(directory, playground):
        click.echo(f"{directory}|{playground}")

    result = runner.invoke(command, ["--directory", str(package_dir), "--playground", "demo"])

    assert result.exit_code == 0
    assert result.output.strip() == f"{package_dir.resolve()}|demo"


def test_project_command_config_helpers(tmp_path):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / mod.PROJECT_CONFIG).write_text(
        yaml.safe_dump(
            {
                "servers": [
                    {
                        "name": "watch.example.com",
                        "apiurl": "https://watch.example.com",
                        "apikey": "watch-key",
                    },
                    {
                        "name": "install.example.com",
                        "apiurl": "https://install.example.com",
                        "apikey": "install-key",
                    },
                ],
                "watch": {"server": "watch.example.com", "playground": "watch-play", "startup": "install"},
                "install": {"server": "install.example.com", "playground": "install-play"},
            }
        ),
        encoding="utf-8",
    )

    project_cfg, env, watch_config = mod.load_project_command_config(str(package_dir), "watch")
    assert project_cfg == str((package_dir / mod.PROJECT_CONFIG).resolve())
    assert len(env) == 2
    assert watch_config == {"server": "watch.example.com", "playground": "watch-play", "startup": "install"}

    watch_server = mod.resolve_command_server("watch", str(package_dir), ("cfg", []), (None, None), "", True)
    assert watch_server == {
        "name": "watch.example.com",
        "apiurl": "https://watch.example.com",
        "apikey": "watch-key",
        "playground": "watch-play",
        "startup": "install",
    }

    install_server = mod.resolve_command_server("install", str(package_dir), ("cfg", []), (None, None), "", True)
    assert install_server == {
        "name": "install.example.com",
        "apiurl": "https://install.example.com",
        "apikey": "install-key",
        "playground": "install-play",
    }

    fallback_server = mod.resolve_command_server(
        "watch",
        str(tmp_path / "missing"),
        ("cfg", [{"name": "fallback.example.com", "apiurl": "https://fallback.example.com", "apikey": "fallback-key"}]),
        (None, None),
        "",
        True,
    )
    assert fallback_server == {
        "name": "fallback.example.com",
        "apiurl": "https://fallback.example.com",
        "apikey": "fallback-key",
    }


def test_resolve_command_server_with_cleanup_removes_stale_project_reference(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / mod.PROJECT_CONFIG).write_text(
        yaml.safe_dump(
            {
                "install": {"server": "missing.example.com", "playground": "release"},
                "watch": {"server": "missing.example.com", "startup": "install"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: True)

    selected_server = mod.resolve_command_server_with_cleanup(
        "install",
        str(package_dir),
        ("cfg", [{"name": "fallback.example.com", "apiurl": "https://fallback.example.com", "apikey": "key"}]),
        (None, None),
        "",
        True,
    )

    assert selected_server == {
        "name": "fallback.example.com",
        "apiurl": "https://fallback.example.com",
        "apikey": "key",
        "playground": "release",
    }
    project_data = yaml.safe_load((package_dir / mod.PROJECT_CONFIG).read_text(encoding="utf-8"))
    assert project_data["install"] == {"playground": "release"}
    assert project_data["watch"] == {"startup": "install"}


def test_resolve_command_server_with_cleanup_preserves_failure_when_declined(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / mod.PROJECT_CONFIG).write_text(
        yaml.safe_dump(
            {
                "install": {"server": "missing.example.com", "playground": "release"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: False)

    with pytest.raises(click.BadParameter, match='Server "missing.example.com" was not found.'):
        mod.resolve_command_server_with_cleanup(
            "install",
            str(package_dir),
            ("cfg", [{"name": "fallback.example.com", "apiurl": "https://fallback.example.com", "apikey": "key"}]),
            (None, None),
            "",
            True,
        )


def test_remove_server_references_from_project_config_noop(tmp_path):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / mod.PROJECT_CONFIG).write_text(
        yaml.safe_dump(
            {
                "install": {"server": "other.example.com", "playground": "release"},
                "watch": {"server": "different.example.com", "startup": "install"},
            }
        ),
        encoding="utf-8",
    )

    removed, config_path = mod.remove_server_references_from_project_config(str(package_dir), "missing.example.com")

    assert removed is False
    assert config_path == str((package_dir / mod.PROJECT_CONFIG).resolve())


def test_resolve_command_server_with_cleanup_reraises_non_cleanup_cases(tmp_path, monkeypatch):
    missing_error = click.BadParameter('Server "missing.example.com" was not found.', param_hint="--server")
    monkeypatch.setattr(mod, "resolve_command_server", lambda *args, **kwargs: (_ for _ in ()).throw(missing_error))

    with pytest.raises(click.BadParameter, match='Server "missing.example.com" was not found.'):
        mod.resolve_command_server_with_cleanup("install", str(tmp_path), ("cfg", []), (None, None), "explicit", True)

    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    with pytest.raises(click.BadParameter, match='Server "missing.example.com" was not found.'):
        mod.resolve_command_server_with_cleanup("install", str(package_dir), ("cfg", []), (None, None), "", True)

    (package_dir / mod.PROJECT_CONFIG).write_text(
        yaml.safe_dump({"install": {"server": "missing.example.com"}}),
        encoding="utf-8",
    )
    other_error = click.BadParameter("other error", param_hint="--server")
    monkeypatch.setattr(mod, "resolve_command_server", lambda *args, **kwargs: (_ for _ in ()).throw(other_error))
    with pytest.raises(click.BadParameter, match="other error"):
        mod.resolve_command_server_with_cleanup("install", str(package_dir), ("cfg", []), (None, None), "", True)


def test_resolve_command_server_with_cleanup_reraises_when_cleanup_fails(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    config_path = package_dir / mod.PROJECT_CONFIG
    config_path.write_text(
        yaml.safe_dump({"install": {"server": "missing.example.com"}}),
        encoding="utf-8",
    )

    missing_error = click.BadParameter('Server "missing.example.com" was not found.', param_hint="--server")
    monkeypatch.setattr(mod, "resolve_command_server", lambda *args, **kwargs: (_ for _ in ()).throw(missing_error))
    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        mod,
        "remove_server_references_from_project_config",
        lambda directory, server_name: (False, str(config_path.resolve())),
    )

    with pytest.raises(click.BadParameter, match='Server "missing.example.com" was not found.'):
        mod.resolve_command_server_with_cleanup("install", str(package_dir), ("cfg", []), (None, None), "", True)


def test_project_config_load_save_error_paths(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / mod.PROJECT_CONFIG).write_text("servers: invalid\n", encoding="utf-8")

    with pytest.raises(click.BadParameter):
        mod.load_or_create_project_config(str(package_dir))

    messages = []
    monkeypatch.setattr(mod.click, "echo", messages.append)
    monkeypatch.setattr(
        mod.yaml,
        "dump",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")),
    )

    assert mod.save_project_config(str(package_dir / mod.PROJECT_CONFIG), [], {"install": {}, "watch": {}}) is False
    assert "Unable to save" in messages[0]


def test_config_target_prompt_helpers(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")

    messages = []
    monkeypatch.setattr(mod.click, "echo", messages.append)

    scope_answers = iter(["maybe", "g", "LOCAL"])
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: next(scope_answers))
    assert mod.prompt_for_config_scope() == "global"
    assert mod.prompt_for_config_scope() == "local"
    assert 'Please enter "g", "global", "l", or "local".' in messages

    playground_answers = iter(["  demo  ", "   "])
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: next(playground_answers))
    assert mod.prompt_for_optional_playground() == "demo"
    assert mod.prompt_for_optional_playground() is None

    command_playground_answers = iter(["  release  ", " "])
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: next(command_playground_answers))
    assert mod.prompt_for_command_playground("install") == "release"
    assert mod.prompt_for_command_playground("watch") is None

    confirm_answers = iter([True, False, True, False])
    monkeypatch.setattr(click, "confirm", lambda *args, **kwargs: next(confirm_answers))
    assert mod.prompt_for_command_default("install") is True
    assert mod.prompt_for_command_default("watch") is False
    assert mod.prompt_for_watch_startup() == "install"
    assert mod.prompt_for_watch_startup() is None

    directory_answers = iter([str(tmp_path / "missing"), ""])
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: next(directory_answers))
    assert mod.prompt_for_optional_directory() is None
    assert any("does not exist" in message for message in messages)

    assert mod.resolve_project_config_directory(str(package_dir)) == str(package_dir.resolve())

    monkeypatch.chdir(package_dir)
    assert mod.resolve_project_config_directory(None) == str(package_dir.resolve())

    messages.clear()
    invalid_cwd = tmp_path / "not-a-package"
    invalid_cwd.mkdir()
    prompted_directories = iter([str(tmp_path / "still-missing"), str(package_dir)])
    monkeypatch.chdir(invalid_cwd)
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: next(prompted_directories))
    assert mod.resolve_project_config_directory(None) == str(package_dir.resolve())
    assert any("does not exist" in message for message in messages)

    with pytest.raises(click.BadParameter):
        mod.resolve_config_target("global.yml", True, False, None)


def test_apply_project_command_defaults():
    sections = {
        "install": {"server": "old-install", "playground": "old-play"},
        "watch": {"server": "old-watch", "playground": "old-watch-play", "startup": "install"},
    }

    updated = mod.apply_project_command_defaults(
        sections=sections,
        server_name="new.example.com",
        configure_install=True,
        install_playground=None,
        configure_watch=True,
        watch_playground="testing",
        watch_startup=None,
    )

    assert updated["install"] == {"server": "new.example.com"}
    assert updated["watch"] == {"server": "new.example.com", "playground": "testing"}
    assert sections["watch"]["startup"] == "install"


def test_ensure_api_credentials_retries(monkeypatch):
    prompts = iter(
        [
            ("https://first.example.com", "bad-key"),
            ("https://second.example.com", "good-key"),
        ]
    )
    monkeypatch.setattr(mod, "prompt_for_api", lambda **kwargs: next(prompts))
    attempts = []
    monkeypatch.setattr(
        mod,
        "test_apiurl_apikey",
        lambda **kwargs: attempts.append(kwargs) or kwargs["apikey"] == "good-key",
    )

    apiurl, apikey = mod.ensure_api_credentials(None, None)

    assert (apiurl, apikey) == ("https://second.example.com", "good-key")
    assert attempts == [
        {"apiurl": "https://first.example.com", "apikey": "bad-key"},
        {"apiurl": "https://second.example.com", "apikey": "good-key"},
    ]


def test_project_command_config_error_and_merge_branches(tmp_path, monkeypatch):
    with pytest.raises(click.BadParameter):
        mod.load_project_command_config(str(tmp_path / "missing"), "watch")

    assert mod.parse_project_command_config([]) == ([], {"install": {}, "watch": {}})
    assert mod.parse_project_command_config({"servers": None, "install": None, "watch": {}}) == (
        [],
        {"install": {}, "watch": {}},
    )

    with pytest.raises(ValueError):
        mod.parse_project_command_config("not-a-dict")

    with pytest.raises(ValueError):
        mod.parse_project_command_config({"servers": "not-a-list"})

    with pytest.raises(ValueError):
        mod.parse_project_command_config({"servers": [], "watch": []})

    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    config_path = package_dir / mod.PROJECT_CONFIG
    config_path.write_text("servers: wrong\n", encoding="utf-8")
    with pytest.raises(click.BadParameter, match="usable project config"):
        mod.load_project_command_config(str(package_dir), "watch")

    config_path.write_text("servers: []\n", encoding="utf-8")

    def raise_bad_parameter(*args, **kwargs):
        raise click.BadParameter("bad project config")

    monkeypatch.setattr(mod, "parse_project_command_config", raise_bad_parameter)
    with pytest.raises(click.BadParameter, match="bad project config"):
        mod.load_project_command_config(str(package_dir), "watch")

    merged = mod.merge_command_config({}, {"apiurl": "https://named.example.com", "playground": "demo"})
    assert merged == {
        "apiurl": "https://named.example.com",
        "name": "named.example.com",
        "playground": "demo",
    }

    merged_with_api = mod.merge_command_config(
        {"name": "original", "apiurl": "https://original.example.com", "apikey": "original-key"},
        {
            "server": "ignored",
            "name": "override",
            "apiurl": "https://override.example.com",
            "apikey": "override-key",
            "playground": "demo",
        },
        api_provided=True,
    )
    assert merged_with_api == {
        "name": "original",
        "apiurl": "https://original.example.com",
        "apikey": "original-key",
        "playground": "demo",
    }


def test_resolve_command_server_without_project_config(monkeypatch):
    env = [{"name": "fallback.example.com", "apiurl": "https://fallback.example.com", "apikey": "key"}]

    monkeypatch.setattr(mod.os.path, "isfile", lambda path: (_ for _ in ()).throw(AssertionError(path)))

    selected_server = mod.resolve_command_server("install", "/tmp/pkg", ("cfg", env), (None, None), "", False)

    assert selected_server == env[0]


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
        "  apiurl: https://example.com",
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
    assert mod.wait_for_server(False, "task-4", "key", "https://example.com", "1.5.3") is False


def test_wait_for_server_reraises_thread_exception(monkeypatch):
    monkeypatch.setattr(mod.time, "time", lambda: 1)
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(mod.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("boom")))
    with pytest.raises(ValueError):
        mod.wait_for_server(False, "task", "key", "https://example.com", "1.5.3")


def test_package_installer_reads_pyproject_metadata(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / "pyproject.toml").write_text(
        "[project]\nname = 'docassemble.test'\ndependencies = ['dep>=1.0']\n",
        encoding="utf-8",
    )
    (package_dir / "docassemble" / "test" / "data" / "questions").mkdir(parents=True)
    (package_dir / "docassemble" / "test" / "data" / "questions" / "interview.yml").write_text(
        "---\n", encoding="utf-8"
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
        lambda *args, **kwargs: DummyResponse(
            status_code=200,
            json_data=[
                {"name": "dep", "version": "1.2"},
                {"name": "docassemble.test", "version": "0.0.1"},
            ],
        ),
    )
    posts = iter([DummyResponse(status_code=200, json_data={"task_id": "task"}), DummyResponse(status_code=204)])
    monkeypatch.setattr(mod.requests, "post", lambda *args, **kwargs: next(posts))
    monkeypatch.setattr(mod, "wait_for_server", lambda **kwargs: True)

    assert mod.package_installer(str(package_dir), "https://example.com", "key", playground=None, restart="auto") == 0


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
    monkeypatch.setattr(mod, "resolve_command_server", lambda *args, **kwargs: selected_server)
    monkeypatch.setattr(mod, "package_installer", lambda **kwargs: calls.append(kwargs) or 0)

    assert mod.install.callback(str(package_dir), ("cfg", []), False, (None, None), "", None, "auto", False, False) == 0
    assert calls[0]["directory"] == str(package_dir)
    assert calls[0]["dry_run"] is False
    assert calls[0]["show_files"] is False
    assert mod.install.callback(str(package_dir), ("cfg", []), False, (None, None), "", None, "auto", True, True) == 0
    assert calls[1]["dry_run"] is True
    assert calls[1]["show_files"] is True
    assert mod.calculate_checksum(str(target))

    monkeypatch.setattr(mod, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")), raising=False)
    assert mod.calculate_checksum(str(target)) == ""


def test_package_installer_dry_run_has_no_writes(tmp_path, monkeypatch, capsys):
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

    post_calls = []
    wait_calls = []

    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(mod.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no get")))
    monkeypatch.setattr(mod.requests, "post", lambda *args, **kwargs: post_calls.append(args) or DummyResponse())
    monkeypatch.setattr(mod, "wait_for_server", lambda *args, **kwargs: wait_calls.append(kwargs) or True)

    assert (
        mod.package_installer(
            str(package_dir), "https://example.com", "key", playground=None, restart="yes", dry_run=True
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Dry run: no changes were sent." in output
    assert "Would restart server: yes" in output
    assert "Files to upload:" not in output
    assert "module.py" not in output
    assert "Use --show-files to list the files in the preview." in output

    assert (
        mod.package_installer(
            str(package_dir),
            "https://example.com",
            "key",
            playground=None,
            restart="yes",
            dry_run=True,
            show_files=True,
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Files to upload:" in output
    assert "module.py" in output
    assert post_calls == []
    assert wait_calls == []


def test_upload_playground_files_dry_run_has_no_writes(tmp_path, monkeypatch, capsys):
    live_file = tmp_path / "docassemble" / "test" / "data" / "questions" / "live.yml"
    live_file.parent.mkdir(parents=True, exist_ok=True)
    live_file.write_text("---\n", encoding="utf-8")

    post_calls = []
    monkeypatch.setattr(mod.requests, "post", lambda *args, **kwargs: post_calls.append(args) or DummyResponse())

    assert (
        mod.upload_playground_files(
            "https://example.com",
            "key",
            "demo",
            {str(live_file): "modified"},
            dry_run=True,
            show_files=False,
        )
        is True
    )
    output = capsys.readouterr().out
    assert "Dry run: no changes were sent." in output
    assert "questions: 1" in output
    assert str(live_file) not in output
    assert "Use --show-files to list the files in the preview." in output

    assert (
        mod.upload_playground_files(
            "https://example.com",
            "key",
            "demo",
            {str(live_file): "modified"},
            dry_run=True,
            show_files=True,
        )
        is True
    )
    output = capsys.readouterr().out
    assert "Files to upload:" in output
    assert str(live_file) in output
    assert post_calls == []


def test_dry_run_helper_zero_file_paths(capsys):
    mod.show_dry_run_file_hint(True)
    assert capsys.readouterr().out == ""

    mod.show_dry_run_package_install(playground=None, should_restart=False, archived_files=[], show_files=False)
    output = capsys.readouterr().out
    assert "Would upload 0 file(s) to Package." in output
    assert "Use --show-files" not in output

    mod.show_dry_run_playground_upload(
        "demo",
        {"questions": [], "sources": [], "static": [], "templates": [], "modules": []},
        show_files=False,
    )
    output = capsys.readouterr().out
    assert 'Would upload 0 changed file(s) to Playground "demo".' in output
    assert "Use --show-files" not in output


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
    coverage_file = no_gitignore_dir / ".coverage.hostname.pid123"
    coverage_file.write_text("data", encoding="utf-8")
    assert bool(mod.matches_ignore_patterns(str(coverage_file), str(no_gitignore_dir))) is True

    mod.GITMATCH_COMPILED = None
    gitignore_dir = tmp_path / "reloadable"
    gitignore_dir.mkdir()
    switched_file = gitignore_dir / "switch.txt"
    switched_file.write_text("value", encoding="utf-8")
    (gitignore_dir / ".gitignore").write_text("", encoding="utf-8")
    assert bool(mod.matches_ignore_patterns(str(switched_file), str(gitignore_dir))) is False
    (gitignore_dir / ".gitignore").write_text("switch.txt\n", encoding="utf-8")
    assert bool(mod.matches_ignore_patterns(str(switched_file), str(gitignore_dir))) is True

    mod.GITMATCH_COMPILED = None
    watchignore_dir = tmp_path / "watchignore"
    watchignore_dir.mkdir()
    watched_test_file = watchignore_dir / "tests" / "example_test.py"
    watched_test_file.parent.mkdir(parents=True)
    watched_test_file.write_text("print('watch')\n", encoding="utf-8")
    (watchignore_dir / mod.WATCH_IGNORE_FILE).write_text("tests/\n", encoding="utf-8")
    assert bool(mod.matches_ignore_patterns(str(watched_test_file), str(watchignore_dir))) is True

    kept_test_file = watchignore_dir / "tests" / "keep.py"
    kept_test_file.write_text("print('keep')\n", encoding="utf-8")
    (watchignore_dir / mod.WATCH_IGNORE_FILE).write_text("tests/*.py\n!tests/keep.py\n", encoding="utf-8")
    assert bool(mod.matches_ignore_patterns(str(watched_test_file), str(watchignore_dir))) is True
    assert bool(mod.matches_ignore_patterns(str(kept_test_file), str(watchignore_dir))) is False

    changed_watchignore_file = watchignore_dir / "questions" / "main.yml"
    changed_watchignore_file.parent.mkdir(parents=True, exist_ok=True)
    changed_watchignore_file.write_text("---\n", encoding="utf-8")
    assert bool(mod.matches_ignore_patterns(str(changed_watchignore_file), str(watchignore_dir))) is False
    (watchignore_dir / mod.WATCH_IGNORE_FILE).write_text("tests/\nquestions/\n", encoding="utf-8")
    assert bool(mod.matches_ignore_patterns(str(changed_watchignore_file), str(watchignore_dir))) is True

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
    assert mod.LAST_MODIFIED == {"time": 123, "files": {event.src_path: {"created": True}}, "restart": True}

    mod.FILE_CHECKSUMS[event.src_path] = "checksum:module.py"
    mod.LAST_MODIFIED = {"time": 0, "files": {}, "restart": False}
    handler.on_any_event(SimpleNamespace(is_directory=False, event_type="modified", src_path=event.src_path))
    assert mod.LAST_MODIFIED == {"time": 0, "files": {}, "restart": False}

    delete_event = SimpleNamespace(is_directory=False, event_type="deleted", src_path=event.src_path)
    handler.on_any_event(delete_event)
    assert mod.LAST_MODIFIED == {"time": 123, "files": {event.src_path: {"deleted": True}}, "restart": True}


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
        "resolve_command_server",
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
    mod.LAST_MODIFIED = {"time": 1, "files": {str(package_dir / "file.yml"): {"modified": True}}, "restart": True}
    monkeypatch.setattr(mod, "WATCH_SETTLE_DELAY", 0)

    sleep_calls = {"count": 0}

    def fake_sleep(seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] > 1:
            raise KeyboardInterrupt("stop")

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)

    result = mod.watch.callback(str(package_dir), ("cfg", []), False, (None, None), "", None, "auto", 0, False, False)

    assert result == '\nStopping "docassemblecli3 watch".'
    assert observer.stopped is True
    assert observer.joined is True
    assert installs[0]["playground"] == "stored-playground"
    assert installs[1]["restart"] == "yes"


def test_install_and_watch_use_project_config_defaults(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()

    install_calls = []
    monkeypatch.setattr(
        mod,
        "resolve_command_server",
        lambda command_name, *args, **kwargs: {
            "name": f"{command_name}.example.com",
            "apiurl": f"https://{command_name}.example.com",
            "apikey": f"{command_name}-key",
            "playground": f"{command_name}-playground",
        },
    )
    monkeypatch.setattr(mod, "package_installer", lambda **kwargs: install_calls.append(kwargs) or 0)

    assert mod.install.callback(str(package_dir), ("cfg", []), True, (None, None), "", None, "auto", False, False) == 0
    assert install_calls[0]["playground"] == "install-playground"

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
    mod.LAST_MODIFIED = {"time": 1, "files": {str(package_dir / "file.yml"): {"modified": True}}, "restart": False}
    monkeypatch.setattr(mod, "WATCH_SETTLE_DELAY", 0)

    sleep_calls = {"count": 0}

    def fake_sleep(seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] > 1:
            raise KeyboardInterrupt("stop")

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)

    assert mod.watch.callback(str(package_dir), ("cfg", []), True, (None, None), "", None, "auto", 0, False, False) == (
        '\nStopping "docassemblecli3 watch".'
    )
    assert install_calls[-1]["playground"] == "watch-playground"


def test_watch_helpers_and_incremental_playground_upload(tmp_path, monkeypatch):
    questions_file = tmp_path / "docassemble" / "test" / "data" / "questions" / "a.yml"
    module_file = tmp_path / "docassemble" / "test" / "module.py"
    deleted_file = tmp_path / "docassemble" / "test" / "data" / "questions" / "deleted.yml"
    questions_file.parent.mkdir(parents=True)
    module_file.parent.mkdir(parents=True, exist_ok=True)
    questions_file.write_text("---\n", encoding="utf-8")
    module_file.write_text("value = 1\n", encoding="utf-8")
    changed_files = {
        str(questions_file): "modified",
        str(module_file): "created",
        str(deleted_file): "deleted",
    }
    assert mod.deduplicate_watch_events({"a": {"modified": True}, "b": {"created": True}, "c": {"deleted": True}}) == {
        "a": "modified",
        "b": "created",
        "c": "deleted",
    }
    uploads = mod.classify_playground_paths(changed_files)
    assert uploads["questions"] == [str(questions_file)]
    assert uploads["modules"] == [str(module_file)]

    calls = []
    monkeypatch.setattr(
        mod.requests,
        "post",
        lambda url, data=None, files=None, headers=None, timeout=None: (
            calls.append((url, data, set(files.keys()))) or DummyResponse(status_code=204)
        ),
    )
    assert mod.upload_playground_files("https://example.com", "key", "demo", changed_files) is True
    assert calls[0][0].endswith("/api/playground")
    assert calls[0][1]["folder"] == "questions"
    assert calls[-1][1]["restart"] == "1"


def test_playground_classification_and_upload_error_paths(tmp_path, monkeypatch):
    deleted_python = {str(tmp_path / "docassemble" / "test" / "module.py"): "deleted"}
    assert mod.classify_playground_paths(deleted_python) is None
    assert mod.classify_playground_paths({str(tmp_path / "other.txt"): "modified"}) is None
    assert (
        mod.upload_playground_files("https://example.com", "key", "demo", {str(tmp_path / "other.txt"): "modified"})
        is False
    )

    missing_file = tmp_path / "docassemble" / "test" / "data" / "questions" / "missing.yml"
    changed_files = {str(missing_file): "modified"}
    assert mod.upload_playground_files("https://example.com", "key", "demo", changed_files) is True

    live_file = tmp_path / "docassemble" / "test" / "data" / "questions" / "live.yml"
    live_file.parent.mkdir(parents=True, exist_ok=True)
    live_file.write_text("---\n", encoding="utf-8")
    changed_files = {str(live_file): "modified"}
    monkeypatch.setattr(
        mod.requests, "post", lambda *args, **kwargs: DummyResponse(status_code=200, json_data={"task_id": "task"})
    )
    monkeypatch.setattr(mod, "wait_for_server", lambda *args, **kwargs: False)
    assert mod.upload_playground_files("https://example.com", "key", "demo", changed_files) is False

    monkeypatch.setattr(mod.requests, "post", lambda *args, **kwargs: DummyResponse(status_code=500, text="bad"))
    monkeypatch.setattr(mod, "wait_for_server", lambda *args, **kwargs: True)
    assert mod.upload_playground_files("https://example.com", "key", "demo", changed_files) is False

    monkeypatch.setattr(mod.requests, "post", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(click.ClickException):
        mod.upload_playground_files("https://example.com", "key", "demo", changed_files)


def test_upload_playground_files_default_project_success(tmp_path, monkeypatch):
    live_file = tmp_path / "docassemble" / "test" / "data" / "questions" / "live.yml"
    live_file.parent.mkdir(parents=True, exist_ok=True)
    live_file.write_text("---\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        mod.requests,
        "post",
        lambda url, data=None, files=None, headers=None, timeout=None: (
            calls.append(data.copy()) or DummyResponse(status_code=200, json_data={"task_id": "task"})
        ),
    )
    monkeypatch.setattr(mod, "wait_for_server", lambda *args, **kwargs: True)
    assert mod.upload_playground_files("https://example.com", "key", "default", {str(live_file): "modified"}) is True
    assert "project" not in calls[0]


def test_create_command(tmp_path, monkeypatch):
    prompts = iter(["", "", "", "", "Custom", "1.2.3"])
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: next(prompts))

    output_dir = tmp_path / "docassemble-demo"
    assert mod.create.callback("demo", None, None, None, None, None, None, str(output_dir)) == 0
    assert (output_dir / "setup.py").is_file()
    assert (output_dir / "pyproject.toml").is_file()
    pyproject_text = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires = ["setuptools==80.9.0"]' in pyproject_text
    assert "LicenseRef-Custom" in pyproject_text
    assert 'license-files = ["LICENSE"]' in pyproject_text
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
    monkeypatch.setattr(mod, "test_apiurl_apikey", lambda **kwargs: True)
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

    config_path.write_text(
        yaml.safe_dump([{"name": "example.com", "apiurl": "https://example.com", "apikey": "key"}]),
        encoding="utf-8",
    )

    monkeypatch.setattr(mod, "save_config", lambda **kwargs: True)
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: "example.com")
    monkeypatch.setattr(mod, "prompt_for_config_scope", lambda: "global")
    assert mod.remove.callback(str(config_path), False, False, None, None) is None

    mod.show.callback(str(config_path), False, False, None)

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


def test_config_add_prompts_for_global_target(tmp_path, monkeypatch, runner):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")

    default_config = tmp_path / "global.yml"
    monkeypatch.setattr(mod, "DEFAULT_CONFIG", str(default_config))
    monkeypatch.setattr(mod, "prompt_for_config_scope", lambda: "global")
    monkeypatch.setattr(mod, "prompt_for_optional_directory", lambda: str(package_dir))
    monkeypatch.setattr(mod, "prompt_for_optional_playground", lambda: "demo")
    monkeypatch.setattr(mod, "prompt_for_api", lambda **kwargs: ("https://global.example.com", "key"))
    monkeypatch.setattr(mod, "test_apiurl_apikey", lambda **kwargs: True)

    saved = []
    monkeypatch.setattr(mod, "save_config", lambda cfg, env: saved.append((cfg, list(env))) or True)

    result = runner.invoke(mod.cli, ["config", "add"])

    assert result.exit_code == 0
    assert saved[0][0] == str(default_config.resolve())
    assert saved[0][1][0]["name"] == "global.example.com"
    assert saved[0][1][0]["directory"] == str(package_dir.resolve())
    assert saved[0][1][0]["playground"] == "demo"


def test_config_add_prompts_for_local_project_config(tmp_path, monkeypatch, runner):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")

    monkeypatch.chdir(package_dir)
    monkeypatch.setattr(mod, "prompt_for_config_scope", lambda: "local")
    monkeypatch.setattr(
        mod,
        "prompt_for_optional_directory",
        lambda: (_ for _ in ()).throw(AssertionError("directory prompt should not be used for local config")),
    )
    monkeypatch.setattr(mod, "prompt_for_optional_playground", lambda: "demo")
    command_defaults = iter([True, True])
    monkeypatch.setattr(mod, "prompt_for_command_default", lambda command_name: next(command_defaults))
    command_playgrounds = iter(["release", "testing"])
    monkeypatch.setattr(mod, "prompt_for_command_playground", lambda command_name: next(command_playgrounds))
    monkeypatch.setattr(mod, "prompt_for_watch_startup", lambda: "install")
    monkeypatch.setattr(mod, "prompt_for_api", lambda **kwargs: ("https://local.example.com", "key"))
    monkeypatch.setattr(mod, "test_apiurl_apikey", lambda **kwargs: True)

    result = runner.invoke(mod.cli, ["config", "add"])

    assert result.exit_code == 0
    project_data = yaml.safe_load((package_dir / mod.PROJECT_CONFIG).read_text(encoding="utf-8"))
    assert project_data["servers"][0]["name"] == "local.example.com"
    assert project_data["servers"][0]["playground"] == "demo"
    assert "directory" not in project_data["servers"][0]
    assert project_data["install"] == {"server": "local.example.com", "playground": "release"}
    assert project_data["watch"] == {
        "server": "local.example.com",
        "playground": "testing",
        "startup": "install",
    }


def test_config_add_project_config_cli_skips_prompts(tmp_path, monkeypatch, runner):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")

    monkeypatch.setattr(
        mod,
        "prompt_for_config_scope",
        lambda: (_ for _ in ()).throw(AssertionError("config scope prompt should be skipped")),
    )
    monkeypatch.setattr(
        mod,
        "prompt_for_optional_playground",
        lambda: (_ for _ in ()).throw(AssertionError("playground prompt should be skipped")),
    )
    monkeypatch.setattr(
        mod,
        "prompt_for_api",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("API prompt should be skipped")),
    )
    monkeypatch.setattr(
        mod,
        "prompt_for_command_default",
        lambda command_name: (_ for _ in ()).throw(AssertionError("command default prompt should be skipped")),
    )
    monkeypatch.setattr(
        mod,
        "prompt_for_command_playground",
        lambda command_name: (_ for _ in ()).throw(AssertionError("command playground prompt should be skipped")),
    )
    monkeypatch.setattr(
        mod,
        "prompt_for_watch_startup",
        lambda: (_ for _ in ()).throw(AssertionError("watch startup prompt should be skipped")),
    )
    monkeypatch.setattr(mod, "test_apiurl_apikey", lambda **kwargs: True)

    result = runner.invoke(
        mod.cli,
        [
            "config",
            "add",
            "--project-config",
            "--api",
            "https://explicit.example.com",
            "key",
            "--directory",
            str(package_dir),
            "--playground",
            "demo",
            "--install-default",
            "--install-playground",
            "release",
            "--watch-default",
            "--watch-playground",
            "testing",
            "--watch-startup",
            "install",
        ],
    )

    assert result.exit_code == 0
    project_data = yaml.safe_load((package_dir / mod.PROJECT_CONFIG).read_text(encoding="utf-8"))
    assert project_data["servers"][0] == {
        "apiurl": "https://explicit.example.com",
        "apikey": "key",
        "name": "explicit.example.com",
        "directory": str(package_dir.resolve()),
        "playground": "demo",
    }
    assert project_data["install"] == {"server": "explicit.example.com", "playground": "release"}
    assert project_data["watch"] == {
        "server": "explicit.example.com",
        "playground": "testing",
        "startup": "install",
    }


def test_config_add_local_command_options_imply_project_config(tmp_path, monkeypatch, runner):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")

    monkeypatch.setattr(
        mod,
        "prompt_for_config_scope",
        lambda: (_ for _ in ()).throw(AssertionError("config scope prompt should be skipped")),
    )
    monkeypatch.setattr(mod, "prompt_for_optional_playground", lambda: None)
    monkeypatch.setattr(mod, "prompt_for_command_playground", lambda command_name: None)
    monkeypatch.setattr(mod, "prompt_for_watch_startup", lambda: None)
    monkeypatch.setattr(mod, "test_apiurl_apikey", lambda **kwargs: True)

    result = runner.invoke(
        mod.cli,
        [
            "config",
            "add",
            "--api",
            "https://implicit.example.com",
            "key",
            "--directory",
            str(package_dir),
            "--install-default",
            "--watch-default",
        ],
    )

    assert result.exit_code == 0
    project_data = yaml.safe_load((package_dir / mod.PROJECT_CONFIG).read_text(encoding="utf-8"))
    assert project_data["install"] == {"server": "implicit.example.com"}
    assert project_data["watch"] == {"server": "implicit.example.com"}


def test_config_add_rejects_local_command_options_with_global_config(tmp_path, monkeypatch, runner):
    config_path = tmp_path / "global.yml"
    config_path.write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(mod, "test_apiurl_apikey", lambda **kwargs: True)

    result = runner.invoke(
        mod.cli,
        [
            "config",
            "add",
            "--global-config",
            "--api",
            "https://global.example.com",
            "key",
            "--install-default",
        ],
    )

    assert result.exit_code != 0
    assert "package-local project config" in result.output


def test_config_add_rejects_conflicting_local_command_options(tmp_path, runner):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")

    install_result = runner.invoke(
        mod.cli,
        [
            "config",
            "add",
            "--project-config",
            "--directory",
            str(package_dir),
            "--no-install-default",
            "--install-playground",
            "release",
        ],
    )
    assert install_result.exit_code != 0
    assert "--no-install-default" in install_result.output

    watch_result = runner.invoke(
        mod.cli,
        [
            "config",
            "add",
            "--project-config",
            "--directory",
            str(package_dir),
            "--no-watch-default",
            "--watch-startup",
            "install",
        ],
    )
    assert watch_result.exit_code != 0
    assert "--no-watch-default" in watch_result.output


def test_config_add_watch_startup_none_clears_startup(tmp_path, monkeypatch, runner):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")
    (package_dir / mod.PROJECT_CONFIG).write_text(
        yaml.safe_dump(
            {
                "servers": [],
                "install": {},
                "watch": {"server": "old.example.com", "startup": "install"},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(mod, "test_apiurl_apikey", lambda **kwargs: True)

    result = runner.invoke(
        mod.cli,
        [
            "config",
            "add",
            "--project-config",
            "--api",
            "https://watch-none.example.com",
            "key",
            "--directory",
            str(package_dir),
            "--playground",
            "demo",
            "--no-install-default",
            "--watch-default",
            "--watch-playground",
            "testing",
            "--watch-startup",
            "none",
        ],
    )

    assert result.exit_code == 0
    project_data = yaml.safe_load((package_dir / mod.PROJECT_CONFIG).read_text(encoding="utf-8"))
    assert project_data["watch"] == {"server": "watch-none.example.com", "playground": "testing"}


def test_config_add_project_config_save_failure(tmp_path, monkeypatch, runner):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")

    monkeypatch.setattr(mod, "test_apiurl_apikey", lambda **kwargs: True)
    monkeypatch.setattr(mod, "prompt_for_command_default", lambda command_name: False)
    monkeypatch.setattr(mod, "save_project_config", lambda *args, **kwargs: False)

    result = runner.invoke(
        mod.cli,
        [
            "config",
            "add",
            "--project-config",
            "--api",
            "https://explicit.example.com",
            "key",
            "--directory",
            str(package_dir),
            "--playground",
            "demo",
        ],
    )

    assert result.exit_code == 0
    assert "Configuration saved:" not in result.output


def test_config_show_and_remove_local_project_config(tmp_path, monkeypatch, capsys):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")
    (package_dir / mod.PROJECT_CONFIG).write_text(
        yaml.safe_dump(
            {
                "servers": [{"name": "local.example.com", "apiurl": "https://local.example.com", "apikey": "key"}],
                "install": {"server": "local.example.com", "playground": "release"},
                "watch": {"server": "local.example.com", "playground": "testing", "startup": "install"},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.chdir(package_dir)
    monkeypatch.setattr(mod, "prompt_for_config_scope", lambda: "local")

    assert mod.show.callback(None, False, False, None) is None
    output = capsys.readouterr().out
    assert "local.example.com" in output
    assert "install:" in output
    assert "server: local.example.com" in output
    assert "playground: release" in output
    assert "watch:" in output
    assert "playground: testing" in output
    assert "startup: install" in output

    assert mod.remove.callback(None, False, False, None, "local.example.com") is None
    project_data = yaml.safe_load((package_dir / mod.PROJECT_CONFIG).read_text(encoding="utf-8"))
    assert project_data["servers"] == []
    assert project_data["install"] == {"server": "local.example.com", "playground": "release"}
    assert project_data["watch"] == {"server": "local.example.com", "playground": "testing", "startup": "install"}


def test_display_servers_install_playground_and_create_defaults(tmp_path, monkeypatch):
    servers = mod.display_servers(
        [
            {"name": "first", "startup": "install"},
            {"name": "second", "apiurl": "https://second.example.com", "playground": "demo", "directory": "/pkg"},
        ]
    )
    assert servers == [
        "first (default)",
        "  startup: install",
        "second",
        "  apiurl: https://second.example.com",
        "  playground: demo",
        "  directory: /pkg",
    ]

    assert mod.display_project_command_sections(
        {
            "install": {"server": "prod.example.com", "playground": "release"},
            "watch": {"server": "dev.example.com", "playground": "testing", "startup": "install"},
        }
    ) == [
        "install:",
        "  server: prod.example.com",
        "  playground: release",
        "watch:",
        "  server: dev.example.com",
        "  playground: testing",
        "  startup: install",
    ]
    assert mod.display_project_command_sections({"install": {}, "watch": {"server": "dev.example.com"}}) == [
        "watch:",
        "  server: dev.example.com",
    ]

    env = [{"name": "example.com", "apiurl": "https://example.com", "apikey": "key"}]
    updated = mod.add_or_update_env(env=env, apiurl="https://example.com", apikey="key", playground="demo")
    assert updated[0]["playground"] == "demo"

    installs = []
    monkeypatch.setattr(
        mod,
        "resolve_command_server",
        lambda *args, **kwargs: {"name": "example.com", "apiurl": "https://example.com", "apikey": "key"},
    )
    monkeypatch.setattr(mod, "package_installer", lambda **kwargs: installs.append(kwargs) or 0)
    assert mod.install.callback(str(tmp_path), ("cfg", []), False, (None, None), "", "demo", "auto", False, False) == 0
    assert installs[0]["playground"] == "demo"

    prompts = iter(["", "", "", "", "MIT", "0.0.1"])
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: next(prompts))
    monkeypatch.chdir(tmp_path)
    assert mod.create.callback("docassemble.demo", None, None, None, None, None, None, None) == 0
    created = tmp_path / "docassemble-demo"
    assert created.is_dir()
    assert "The MIT License (MIT)" in (created / "LICENSE").read_text(encoding="utf-8")
    assert (created / "pyproject.toml").is_file()


def test_download_and_uninstall_commands(tmp_path, monkeypatch):
    package_name = "docassemble.test"
    archive_path = tmp_path / "archive.zip"
    with mod.zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("docassemble-test/README.md", "hello")

    selected_server = {"name": "example.com", "apiurl": "https://example.com", "apikey": "key"}
    monkeypatch.setattr(mod, "select_server", lambda *args, **kwargs: selected_server)

    def fake_get(url, *args, **kwargs):
        if url.endswith("/api/package"):
            return DummyResponse(
                status_code=200,
                json_data=[
                    {"name": "docassemble.other", "zip_file_number": 8},
                    {"name": package_name, "zip_file_number": 7},
                ],
            )
        if url.endswith("/api/file/7"):
            return DummyResponse(status_code=200, chunks=[archive_path.read_bytes()])
        raise AssertionError(url)

    monkeypatch.setattr(mod.requests, "get", fake_get)
    monkeypatch.chdir(tmp_path)
    assert mod.download.callback(("cfg", []), (None, None), "", None, False, "test") == 0
    assert (tmp_path / "docassemble-test" / "README.md").is_file()

    monkeypatch.setattr(
        mod.requests, "delete", lambda *args, **kwargs: DummyResponse(status_code=200, json_data={"task_id": "task"})
    )
    monkeypatch.setattr(mod, "wait_for_server", lambda *args, **kwargs: True)
    assert mod.uninstall.callback(("cfg", []), (None, None), "", True, "test") == 0


def test_download_and_uninstall_error_paths(tmp_path, monkeypatch):
    selected_server = {"name": "example.com", "apiurl": "https://example.com", "apikey": "key"}
    monkeypatch.setattr(mod, "select_server", lambda *args, **kwargs: selected_server)

    playground_calls = []

    def fake_playground_get(url, params=None, **kwargs):
        playground_calls.append(params)
        return DummyResponse(status_code=404)

    monkeypatch.setattr(mod.requests, "get", fake_playground_get)
    assert mod.download.callback(("cfg", []), (None, None), "", "proj", False, "test") == "Package not found."
    assert playground_calls[0]["project"] == "proj"

    monkeypatch.setattr(mod.requests, "get", lambda *args, **kwargs: DummyResponse(status_code=500))
    assert mod.download.callback(("cfg", []), (None, None), "", None, False, "test") == "Unable to connect to server."

    monkeypatch.setattr(mod.requests, "get", lambda *args, **kwargs: DummyResponse(status_code=200, json_data=[]))
    assert (
        mod.download.callback(("cfg", []), (None, None), "", None, False, "test")
        == "Package installed but is not downloadable."
    )

    monkeypatch.setattr(mod.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(requests.HTTPError("bad")))
    assert mod.download.callback(("cfg", []), (None, None), "", None, False, "test") == "Error downloading package: bad"

    monkeypatch.setattr(mod.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(click.ClickException):
        mod.download.callback(("cfg", []), (None, None), "", None, False, "test")

    monkeypatch.setattr(mod.requests, "delete", lambda *args, **kwargs: DummyResponse(status_code=500, text="bad"))
    assert mod.uninstall.callback(("cfg", []), (None, None), "", False, "test") == "package DELETE returned 500: bad"

    monkeypatch.setattr(
        mod.requests, "delete", lambda *args, **kwargs: DummyResponse(status_code=200, json_data={"task_id": "task"})
    )
    monkeypatch.setattr(mod, "wait_for_server", lambda *args, **kwargs: False)
    assert mod.uninstall.callback(("cfg", []), (None, None), "", False, "test") == 1

    monkeypatch.setattr(mod.requests, "delete", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(click.ClickException):
        mod.uninstall.callback(("cfg", []), (None, None), "", False, "test")


def test_download_playground_success_and_overwrite_guard(tmp_path, monkeypatch):
    selected_server = {"name": "example.com", "apiurl": "https://example.com", "apikey": "key"}
    monkeypatch.setattr(mod, "select_server", lambda *args, **kwargs: selected_server)

    archive_path = tmp_path / "archive.zip"
    with mod.zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("docassemble-test/README.md", "hello")

    monkeypatch.setattr(
        mod.requests,
        "get",
        lambda *args, **kwargs: DummyResponse(status_code=200, chunks=[archive_path.read_bytes()]),
    )
    monkeypatch.chdir(tmp_path)
    assert mod.download.callback(("cfg", []), (None, None), "", "default", False, "test") == 0

    collision_path = tmp_path / "docassemble-test" / "README.md"
    collision_path.write_text("existing", encoding="utf-8")
    assert (
        mod.download.callback(("cfg", []), (None, None), "", "default", False, "test")
        == "Unpacking the package here would overwrite existing files (docassemble-test/README.md). Use --overwrite if you want to overwrite existing files."
    )
    assert mod.download.callback(("cfg", []), (None, None), "", "default", True, "test") == 0


def test_watch_handler_ignores_and_resets_deleted_bucket(tmp_path, monkeypatch):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    file_path = package_dir / "module.py"
    file_path.write_text("value = 1\n", encoding="utf-8")
    handler = mod.WatchHandler(directory=str(package_dir))
    monkeypatch.setattr(mod, "matches_ignore_patterns", lambda **kwargs: False)
    monkeypatch.setattr(mod.time, "time", lambda: 123)
    monkeypatch.setattr(mod, "calculate_checksum", lambda path: "checksum:new")

    assert handler.on_any_event(SimpleNamespace(is_directory=True, event_type="modified")) is None
    assert (
        handler.on_any_event(SimpleNamespace(is_directory=False, event_type="moved", src_path=str(file_path))) is None
    )

    mod.LAST_MODIFIED = {"time": 0, "files": {str(file_path): {"deleted": True}}, "restart": False}
    handler.on_any_event(SimpleNamespace(is_directory=False, event_type="created", src_path=str(file_path)))
    assert mod.LAST_MODIFIED["files"][str(file_path)] == {"created": True}


def test_watch_command_empty_batch_and_incremental_path(tmp_path, monkeypatch):
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
        "resolve_command_server",
        lambda *args, **kwargs: {
            "name": "example.com",
            "apiurl": "https://example.com",
            "apikey": "key",
            "directory": str(package_dir),
            "playground": "stored-playground",
        },
    )
    monkeypatch.setattr(mod, "WATCH_SETTLE_DELAY", 0)

    package_calls = []
    upload_calls = []
    monkeypatch.setattr(mod, "package_installer", lambda **kwargs: package_calls.append(kwargs) or 0)
    monkeypatch.setattr(mod, "upload_playground_files", lambda **kwargs: upload_calls.append(kwargs) or True)

    mod.FULL_INSTALL_DONE = True
    mod.LAST_MODIFIED = {"time": 1, "files": {str(package_dir / "ignored.yml"): {}}, "restart": False}

    sleep_calls = {"count": 0}

    def fake_sleep(seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] == 1:
            mod.LAST_MODIFIED = {
                "time": 1,
                "files": {str(package_dir / "file.yml"): {"modified": True}},
                "restart": False,
            }
            return None
        raise KeyboardInterrupt("stop")

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)
    result = mod.watch.callback(str(package_dir), ("cfg", []), False, (None, None), "", None, "no", 0, False, False)

    assert result == '\nStopping "docassemblecli3 watch".'
    assert package_calls == []
    assert upload_calls[0]["playground"] == "stored-playground"


def test_watch_incremental_upload_announces_installed(tmp_path, monkeypatch, capsys):
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
        "resolve_command_server",
        lambda *args, **kwargs: {
            "name": "example.com",
            "apiurl": "https://example.com",
            "apikey": "key",
            "directory": str(package_dir),
            "playground": "stored-playground",
        },
    )
    monkeypatch.setattr(mod, "WATCH_SETTLE_DELAY", 0)

    package_calls = []
    upload_calls = []
    monkeypatch.setattr(mod, "package_installer", lambda **kwargs: package_calls.append(kwargs) or 0)
    monkeypatch.setattr(mod, "upload_playground_files", lambda **kwargs: upload_calls.append(kwargs) or True)

    mod.FULL_INSTALL_DONE = True
    mod.LAST_MODIFIED = {"time": 1, "files": {str(package_dir / "file.yml"): {"modified": True}}, "restart": False}

    sleep_calls = {"count": 0}

    def fake_sleep(seconds):
        sleep_calls["count"] += 1
        raise KeyboardInterrupt("stop")

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)

    result = mod.watch.callback(str(package_dir), ("cfg", []), False, (None, None), "", None, "no", 0, False, False)

    assert result == '\nStopping "docassemblecli3 watch".'
    assert package_calls == []
    assert upload_calls[0]["playground"] == "stored-playground"
    assert "Installed.\a" in capsys.readouterr().out


def test_watch_dry_run_uses_incremental_playground_preview(tmp_path, monkeypatch, capsys):
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
        "resolve_command_server",
        lambda *args, **kwargs: {
            "name": "example.com",
            "apiurl": "https://example.com",
            "apikey": "key",
            "directory": str(package_dir),
            "playground": "stored-playground",
        },
    )
    monkeypatch.setattr(mod, "WATCH_SETTLE_DELAY", 0)

    package_calls = []
    upload_calls = []
    monkeypatch.setattr(mod, "package_installer", lambda **kwargs: package_calls.append(kwargs) or 0)
    monkeypatch.setattr(mod, "upload_playground_files", lambda **kwargs: upload_calls.append(kwargs) or True)

    mod.FULL_INSTALL_DONE = True
    mod.LAST_MODIFIED = {"time": 1, "files": {str(package_dir / "file.yml"): {"modified": True}}, "restart": False}

    def fake_sleep(seconds):
        raise KeyboardInterrupt("stop")

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)

    result = mod.watch.callback(str(package_dir), ("cfg", []), False, (None, None), "", None, "no", 0, True, False)

    assert result == '\nStopping "docassemblecli3 watch".'
    assert package_calls == []
    assert upload_calls[0]["dry_run"] is True
    assert upload_calls[0]["show_files"] is False
    assert "incremental Playground upload preview complete" in capsys.readouterr().out


def test_watch_startup_install_message_non_dry_run(tmp_path, monkeypatch, capsys):
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
        "resolve_command_server",
        lambda *args, **kwargs: {
            "name": "example.com",
            "apiurl": "https://example.com",
            "apikey": "key",
            "startup": "install",
        },
    )
    monkeypatch.setattr(mod, "package_installer", lambda **kwargs: 0)

    sleep_calls = {"count": 0}

    def fake_sleep(seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] > 1:
            raise KeyboardInterrupt("stop")

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)

    assert mod.watch.callback(str(package_dir), ("cfg", []), False, (None, None), "", None, "no", 0, False, False) == (
        '\nStopping "docassemblecli3 watch".'
    )
    assert "Installing on startup." in capsys.readouterr().out


def test_watch_startup_install_message_dry_run(tmp_path, monkeypatch, capsys):
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
        "resolve_command_server",
        lambda *args, **kwargs: {
            "name": "example.com",
            "apiurl": "https://example.com",
            "apikey": "key",
            "startup": "install",
        },
    )
    monkeypatch.setattr(mod, "package_installer", lambda **kwargs: 0)

    sleep_calls = {"count": 0}

    def fake_sleep(seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] > 1:
            raise KeyboardInterrupt("stop")

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)

    assert mod.watch.callback(str(package_dir), ("cfg", []), False, (None, None), "", None, "no", 0, True, False) == (
        '\nStopping "docassemblecli3 watch".'
    )
    assert "Previewing startup install." in capsys.readouterr().out


def test_watch_command_falls_back_to_package_installer(tmp_path, monkeypatch):
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
        "resolve_command_server",
        lambda *args, **kwargs: {"name": "example.com", "apiurl": "https://example.com", "apikey": "key"},
    )
    monkeypatch.setattr(mod, "WATCH_SETTLE_DELAY", 0)
    installs = []
    monkeypatch.setattr(mod, "package_installer", lambda **kwargs: installs.append(kwargs) or 0)
    mod.FULL_INSTALL_DONE = False
    mod.LAST_MODIFIED = {"time": 1, "files": {str(package_dir / "file.yml"): {"modified": True}}, "restart": False}

    sleep_calls = {"count": 0}

    def fake_sleep(seconds):
        sleep_calls["count"] += 1
        raise KeyboardInterrupt("stop")

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)
    result = mod.watch.callback(str(package_dir), ("cfg", []), False, (None, None), "", None, "no", 0, False, False)

    assert result == '\nStopping "docassemblecli3 watch".'
    assert installs[0]["restart"] == "no"


def test_calculate_checksum_missing_file_is_quiet(capsys, tmp_path):
    missing_file = tmp_path / ".coverage.hostname.pid123"

    assert mod.calculate_checksum(str(missing_file)) == ""
    assert capsys.readouterr().out == ""


def test_create_without_license_omits_project_license(tmp_path, monkeypatch):
    output_dir = tmp_path / "docassemble-demo"
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: "")
    assert (
        mod.create.callback(
            "demo", "Name", "dev@example.com", "Desc", "https://example.com", "", "0.1.0", str(output_dir)
        )
        == 0
    )
    pyproject_text = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert "\nlicense = " not in pyproject_text
    assert "license-files" not in pyproject_text


def test_normalize_license_string_matches_docassemblecli_behavior():
    assert mod.normalize_license_string("MIT") == "MIT"
    assert mod.normalize_license_string("LicenseRef-Private") == "LicenseRef-Private"
    assert mod.normalize_license_string("Custom license 1.0") == "LicenseRef-Customlicense10"
    assert mod.normalize_license_string("") == ""


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
            else DummyResponse(status_code=200, json_data=[])
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
            else DummyResponse(status_code=200, json_data=["demo"])
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


def test_package_installer_playground_requests_have_timeouts_and_progress(tmp_path, monkeypatch, capsys):
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

    requests_seen = []

    def fake_get(url, *args, **kwargs):
        requests_seen.append(("get", url, kwargs.get("timeout")))
        if url.endswith("/api/package"):
            return DummyResponse(status_code=200, json_data=[{"name": "docassemble.base", "version": "1.5.3"}])
        if url.endswith("/api/playground/project"):
            return DummyResponse(status_code=200, json_data=[])
        raise AssertionError(url)

    def fake_post(url, *args, **kwargs):
        requests_seen.append(("post", url, kwargs.get("timeout")))
        if url.endswith("/api/playground/project"):
            return DummyResponse(status_code=204)
        if url.endswith("/api/playground_install"):
            return DummyResponse(status_code=204)
        raise AssertionError(url)

    monkeypatch.setattr(mod.requests, "get", fake_get)
    monkeypatch.setattr(mod.requests, "post", fake_post)

    assert mod.package_installer(str(package_dir), "https://example.com", "key", playground="demo", restart="auto") == 0

    output = capsys.readouterr().out
    assert "Checking Playground project..." in output
    assert 'Creating Playground project "demo"...' in output
    assert "Uploading package to Playground..." in output
    assert ("get", "https://example.com/api/package", 600) in requests_seen
    assert ("get", "https://example.com/api/playground/project", 600) in requests_seen
    assert ("post", "https://example.com/api/playground/project", 600) in requests_seen
    assert ("post", "https://example.com/api/playground_install", 600) in requests_seen


def test_package_installer_skips_project_create_for_existing_playground(tmp_path, monkeypatch, capsys):
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

    requests_seen = []

    def fake_get(url, *args, **kwargs):
        requests_seen.append(("get", url))
        if url.endswith("/api/package"):
            return DummyResponse(status_code=200, json_data=[{"name": "docassemble.base", "version": "1.5.3"}])
        if url.endswith("/api/playground/project"):
            return DummyResponse(status_code=200, json_data=["demo"])
        raise AssertionError(url)

    def fake_post(url, *args, **kwargs):
        requests_seen.append(("post", url))
        if url.endswith("/api/playground_install"):
            return DummyResponse(status_code=204)
        raise AssertionError(url)

    monkeypatch.setattr(mod.requests, "get", fake_get)
    monkeypatch.setattr(mod.requests, "post", fake_post)

    assert mod.package_installer(str(package_dir), "https://example.com", "key", playground="demo", restart="auto") == 0

    output = capsys.readouterr().out
    assert "Checking Playground project..." in output
    assert 'Creating Playground project "demo"...' not in output
    assert ("post", "https://example.com/api/playground/project") not in requests_seen
    assert ("post", "https://example.com/api/playground_install") in requests_seen


def test_http_get_uses_fresh_niquests_session(monkeypatch):
    calls = []

    def fake_native_get(url, **kwargs):
        raise AssertionError("session transport should be used")

    fake_native_get.__module__ = "niquests.api"

    class FakeSession:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def __enter__(self):
            calls.append(("enter", None))
            return self

        def __exit__(self, exc_type, exc, tb):
            calls.append(("exit", exc_type))
            return False

        def get(self, url, **kwargs):
            calls.append(("get", url, kwargs))
            return DummyResponse(status_code=200, json_data={"ok": True})

    monkeypatch.setattr(mod.requests, "get", fake_native_get)
    monkeypatch.setattr(mod.requests, "Session", FakeSession)

    response = mod.http_get("https://example.com/api/package", timeout=12)

    assert response.status_code == 200
    assert calls[0] == ("init", {"disable_http3": True})
    assert calls[2] == ("get", "https://example.com/api/package", {"timeout": 12})


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
        "resolve_command_server",
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
        mod.watch.callback(str(package_dir), ("cfg", []), False, (None, None), "", None, "auto", 0, False, False)
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
            return DummyResponse(status_code=200, json_data=["default"])
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
        "resolve_command_server",
        lambda *args, **kwargs: {"name": "example.com", "apiurl": "https://example.com", "apikey": "key"},
    )
    monkeypatch.setattr(mod, "package_installer", lambda **kwargs: 0)
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: (_ for _ in ()).throw(RuntimeError("stop")))
    mod.LAST_MODIFIED = {"time": 0, "files": {}, "restart": False}

    assert (
        mod.watch.callback(str(package_dir), ("cfg", []), False, (None, None), "", "explicit", "auto", 0, False, False)
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

    config_path = tmp_path / "cfg.yml"
    config_path.write_text(
        yaml.safe_dump([{"name": "example.com", "apiurl": "https://example.com", "apikey": "key"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "save_config", lambda **kwargs: True)
    assert mod.remove.callback(str(config_path), False, False, None, "example.com") is None

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
