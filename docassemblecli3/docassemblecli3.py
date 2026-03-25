import configparser
import datetime
import hashlib
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
import zipfile
from functools import wraps
from urllib.parse import urlparse

import click
import gitmatch
import requests
import yaml
from packaging.licenses import LICENSES as SPDX_LICENSES
from packaging import version as packaging_version
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
import tomllib

global DEFAULT_CONFIG
DEFAULT_CONFIG = os.path.join(os.path.expanduser("~"), ".docassemblecli")

global LAST_MODIFIED
LAST_MODIFIED = {
    "time": 0,
    "files": {},
    "restart": False,
}

global FULL_INSTALL_DONE
FULL_INSTALL_DONE = False

global FILE_CHECKSUMS
FILE_CHECKSUMS = {}

global DEBUG
DEBUG = False

global BELL
BELL = "\a"

global EXCLUDED_DIRECTORIES
EXCLUDED_DIRECTORIES = [".git", "__pycache__", ".mypy_cache", ".venv", ".history", "build"]

global GITMATCH_COMPILED
GITMATCH_COMPILED = None

global GITMATCH_DIRECTORY
GITMATCH_DIRECTORY = None

global GITIGNORE_MTIME
GITIGNORE_MTIME = None

global WATCH_IGNORE_MTIME
WATCH_IGNORE_MTIME = None

global WATCH_SETTLE_DELAY
WATCH_SETTLE_DELAY = 0.6

global GITIGNORE
GITIGNORE = """\
__pycache__/
*.py[cod]
*$py.class
.mypy_cache/
.dmypy.json
dmypy.json
*.egg-info/
.installed.cfg
*.egg
.vscode
*~
~*
*.~lock.*
.#*
.coverage*
en
*/auto
.history/
.idea
.dir-locals.el
.flake8
*.swp
*.swx
*.tmp
*.tmp.*
.DS_Store
.envrc
.env
.venv
env/
venv/
ENV/
env.bak/
venv.bak/
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
share/python-wheels/
"""

global WATCH_IGNORE_FILE
WATCH_IGNORE_FILE = ".dawatchignore"


# -----------------------------------------------------------------------------
# click
# -----------------------------------------------------------------------------


CONTEXT_SETTINGS = dict(help_option_names=["--help", "-h"])


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option()
@click.option(
    "--bell/--no-bell",
    default=True,
    show_default=True,
    help="Play bell sound notification.",
)
@click.option(
    "--color/--no-color",
    "-C/-N",
    default=None,
    show_default=True,
    help="Overrides color auto-detection in interactive terminals.",
)
@click.option("--debug/--no-debug", default=False, hidden=True)
def cli(bell, color, debug):
    """
    Commands for working with docassemble packages and servers.
    """
    if not bell:
        global BELL
        BELL = ""
    CONTEXT_SETTINGS["color"] = color
    if debug:
        global DEBUG
        DEBUG = True


@cli.group(context_settings=CONTEXT_SETTINGS)
def config():
    """
    Manage servers in a docassemblecli config file.
    """
    pass


def common_params_for_api(func):
    @click.option(
        "--api",
        "-a",
        type=(APIURLType(), str),
        default=(None, None),
        help="URL of the docassemble server and API key of the user (admin or developer)",
    )
    @click.option("--server", "-s", metavar="SERVER", default="", help="Specify a server from the config file")
    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper


def common_params_for_config(func):
    @click.option(
        "--config",
        "-c",
        default=DEFAULT_CONFIG,
        type=click.Path(),
        callback=validate_and_load_or_create_config,
        show_default=True,
        help="Specify the config file to use",
    )
    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper


def common_params_for_installation(func):
    @click.option(
        "--directory",
        "-d",
        default=os.getcwd(),
        type=click.Path(),
        callback=validate_package_directory,
        help="Specify package directory [default: current directory]",
    )
    @click.option(
        "--config",
        "-c",
        is_flag=False,
        flag_value="",
        default=DEFAULT_CONFIG,
        type=click.Path(),
        callback=validate_and_load_or_create_config,
        show_default=True,
        help="Specify the config file to use or leave it blank to skip using any config file",
    )
    @click.option(
        "--playground",
        "-p",
        metavar="(PROJECT)",
        is_flag=False,
        flag_value="default",
        help="Install into the default Playground or into the specified Playground project.",
    )
    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper


def common_params_for_runtime_config(func):
    @click.option(
        "--config",
        "-c",
        is_flag=False,
        flag_value="",
        default=DEFAULT_CONFIG,
        type=click.Path(),
        callback=validate_and_load_or_create_config,
        show_default=True,
        help="Specify the config file to use or leave it blank to skip using any config file",
    )
    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper


def common_params_for_directory_and_playground(func):
    @click.option(
        "--directory",
        "-d",
        default=os.getcwd(),
        type=click.Path(),
        callback=validate_package_directory,
        help="Specify package directory [default: current directory]",
    )
    @click.option(
        "--playground",
        "-p",
        metavar="(PROJECT)",
        is_flag=False,
        flag_value="default",
        help="Install into the default Playground or into the specified Playground project.",
    )
    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper


class APIURLType(click.ParamType):
    name = "url"

    def convert(self, value, param, ctx):
        parsed_url = urlparse(value)
        if all([re.search(r"""^https?://[^\s]+$""", value), parsed_url.scheme, parsed_url.netloc]):
            return f"""{parsed_url.scheme}://{parsed_url.netloc}"""
        else:
            self.fail(f""""{value}" is not a valid URL""", param, ctx)


def package_metadata_files_present(directory: str) -> bool:
    return any(
        os.path.isfile(os.path.join(directory, filename)) for filename in ("setup.py", "setup.cfg", "pyproject.toml")
    )


def parse_dependency_strings(dependency_strings) -> dict:
    dependencies = {}
    for dependency_string in dependency_strings:
        if not isinstance(dependency_string, str):
            continue
        dependency_string = dependency_string.strip().rstrip(",")
        if not dependency_string or dependency_string.startswith("#"):
            continue
        dependency_string = dependency_string.split(";", 1)[0].strip()
        mm = re.search(r"""(.*?)(<=|>=|==|<|>)(.*)""", dependency_string)
        if mm:
            dependencies[mm.group(1).strip()] = {
                "installed": False,
                "operator": mm.group(2),
                "version": mm.group(3).strip(),
            }
        else:
            dependencies[dependency_string] = {"installed": False, "operator": None, "version": None}
    return dependencies


def load_package_metadata(directory: str, files: list[str]) -> tuple[str | None, dict]:
    this_package_name = None
    dependencies = {}

    if "pyproject.toml" in files:
        with open(os.path.join(directory, "pyproject.toml"), "rb") as fp:
            data = tomllib.load(fp)
        project = data.get("project", {})
        if isinstance(project, dict):
            if isinstance(project.get("name"), str):
                this_package_name = project["name"].strip()
            dependencies.update(parse_dependency_strings(project.get("dependencies", [])))

    if "setup.cfg" in files:
        parser = configparser.ConfigParser()
        parser.read(os.path.join(directory, "setup.cfg"), encoding="utf-8")
        if not this_package_name and parser.has_option("metadata", "name"):
            this_package_name = parser.get("metadata", "name").strip()
        if parser.has_option("options", "install_requires"):
            dependencies.update(parse_dependency_strings(parser.get("options", "install_requires").splitlines()))

    if "setup.py" in files:
        with open(os.path.join(directory, "setup.py"), "r", encoding="utf-8") as fp:
            setup_text = fp.read()
        if not this_package_name:
            m = re.search(r"""setup\(.*\bname=(["\'])(.*?)(["\'])""", setup_text)
            if m and m.group(1) == m.group(3):
                this_package_name = m.group(2).strip()
        m = re.search(r"""setup\(.*install_requires=\[(.*?)\]""", setup_text, flags=re.DOTALL)
        if m:
            package_texts = [package_text.strip() for package_text in m.group(1).split(",")]
            install_requires = []
            for package_name in package_texts:
                if len(package_name) >= 3 and package_name[0] == package_name[-1] and package_name[0] in ("'", '"'):
                    install_requires.append(package_name[1:-1])
            dependencies.update(parse_dependency_strings(install_requires))

    return this_package_name, dependencies


def normalize_package_name(package: str) -> str:
    package_name = re.sub(r"^docassemble-", "docassemble.", package)
    if not package_name.startswith("docassemble."):
        package_name = "docassemble." + package_name
    return package_name


def normalize_license_string(license_name: str) -> str:
    normalized_license = license_name.strip()
    if not normalized_license:
        return ""
    spdx_license = SPDX_LICENSES.get(normalized_license.lower())
    if spdx_license:
        return spdx_license["id"]
    if re.search(r"^LicenseRef-[A-Za-z\-0-9]+$", normalized_license):
        return normalized_license
    return "LicenseRef-" + re.sub(r"[^A-Za-z\-0-9]", "", normalized_license)


def announce_installed() -> None:
    click.secho(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Installed.{BELL}", fg="green")


def deduplicate_watch_events(file_events: dict) -> dict[str, str]:
    deduplicated = {}
    for file_path, event_types in file_events.items():
        if "created" in event_types:
            deduplicated[file_path] = "created"
        elif "modified" in event_types:
            deduplicated[file_path] = "modified"
        elif "deleted" in event_types:
            deduplicated[file_path] = "deleted"
    return deduplicated


def classify_playground_paths(changed_files: dict[str, str]) -> dict[str, list[str]] | None:
    uploads = {"questions": [], "sources": [], "static": [], "templates": [], "modules": []}
    for file_path, event_type in changed_files.items():
        normalized_path = "/".join(os.path.normpath(file_path).split(os.sep))
        if event_type == "deleted":
            if normalized_path.endswith(".py"):
                return None
            continue
        match = re.search(r"/docassemble/([^/]+)/data/([^/]+)/", normalized_path)
        if match and match.group(2) in uploads and match.group(2) != "modules":
            uploads[match.group(2)].append(file_path)
            continue
        match = re.search(r"/docassemble/([^/]+)/([^/]+)\.py$", normalized_path)
        if match:
            uploads["modules"].append(file_path)
            continue
        return None
    return uploads


def upload_playground_files(apiurl: str, apikey: str, playground: str, changed_files: dict[str, str]) -> bool:
    uploads = classify_playground_paths(changed_files)
    if uploads is None:
        return False

    for folder in ("questions", "sources", "static", "templates", "modules"):
        files_to_upload = uploads[folder]
        if not files_to_upload:
            continue
        for index, file_path in enumerate(files_to_upload):
            post_data = {
                "folder": folder,
                "restart": "1" if folder == "modules" and index == len(files_to_upload) - 1 else "0",
            }
            if playground and playground != "default":
                post_data["project"] = playground
            try:
                with open(file_path, "rb") as fp:
                    response = requests.post(
                        apiurl + "/api/playground",
                        data=post_data,
                        files={"file": fp},
                        headers={"X-API-Key": apikey},
                        timeout=600,
                    )
            except FileNotFoundError:
                continue
            except Exception as err:
                click.secho(f"""\n{err.__class__.__name__}""", fg="red")
                raise click.ClickException(f"""{err}\n""")
            if response.status_code == 200:
                info = response.json()
                if not wait_for_server(True, info["task_id"], apikey, apiurl):
                    return False
            elif response.status_code != 204:
                return False
    return True


def validate_package_directory(ctx, param, directory: str) -> str:
    directory = os.path.abspath(directory)
    if not os.path.exists(directory):
        raise click.BadParameter(f"""Directory "{directory}" does not exist.""")
    if not package_metadata_files_present(directory):
        raise click.BadParameter(
            f"""Directory "{directory}" does not contain a setup.py, setup.cfg, or pyproject.toml file, so it is not the directory of a valid Python package."""
        )
    else:
        return directory


def validate_and_load_or_create_config(ctx, param, config: str) -> tuple[str, list]:
    if not config:
        return (None, [])
    config = os.path.abspath(config)
    if not os.path.isfile(config):
        if config == DEFAULT_CONFIG:
            env = []
            with open(config, "w", encoding="utf-8") as fp:
                yaml.dump(env, fp)
            os.chmod(config, stat.S_IRUSR | stat.S_IWUSR)
        else:
            raise click.BadParameter(f"""{config} doesn't exist.""")
    try:
        with open(config, "r", encoding="utf-8") as fp:
            env = yaml.load(fp, Loader=yaml.FullLoader)
            if not isinstance(env, list):
                raise Exception
    except Exception:
        raise click.BadParameter("File is not a usable docassemblecli config.")
    return (config, env)


# -----------------------------------------------------------------------------
# utility functions
# -----------------------------------------------------------------------------


def name_from_url(url: str) -> str:
    if not url:
        return ""
    return urlparse(url).netloc


def display_servers(env: list = None) -> list[str]:
    if not env:
        return ["No servers found."]
    servers = []
    for idx, item in enumerate(env):
        if idx:
            servers.append(item.get("name", ""))
        else:
            servers.append(item.get("name", "") + " (default)")
        if "playground" in item:
            servers.append(f"""  playground: {item["playground"]}""")
        if "directory" in item:
            servers.append(f"""  directory: {item["directory"]}""")
        if "startup" in item:
            servers.append(f"""  startup: {item["startup"]}""")
    return servers


def select_server(
    cfg: str = None, env: list = None, apiurl: str = None, apikey: str = None, server: str = "", **kwargs
) -> dict:
    if apiurl and apikey:
        return add_server_to_env(cfg=cfg, env=env, apiurl=apiurl, apikey=apikey)[-1]
    if isinstance(env, list):
        if server:
            if not cfg:
                raise click.BadParameter("Cannot be used without a config file.", param_hint="--server")
            else:
                for item in env:
                    if item.get("name", None) == server:
                        return item
                raise click.BadParameter(f"""Server "{server}" was not found.""", param_hint="--server")
        if len(env) > 0:
            if "directory" in kwargs:
                for item in env:
                    if item.get("directory", None) == kwargs["directory"]:
                        return item
            return env[0]
    if "DOCASSEMBLEAPIURL" in os.environ and "DOCASSEMBLEAPIKEY" in os.environ:
        apiurl: str = os.environ["DOCASSEMBLEAPIURL"]
        apikey: str = os.environ["DOCASSEMBLEAPIKEY"]
        return add_or_update_env(apiurl=apiurl, apikey=apikey)[0]
    return add_server_to_env(cfg, env)[0]


def add_or_update_env(
    env: list = None, apiurl: str = "", apikey: str = "", directory: str = "", playground: str = ""
) -> list:
    if not env:
        env: list = []
    apiname: str = name_from_url(apiurl)
    found: bool = False
    for item in env:
        if item.get("name", None) == apiname:
            item["apiurl"] = apiurl
            item["apikey"] = apikey
            if directory:
                item["directory"] = directory
            if playground:
                item["playground"] = playground
            found = True
            click.echo(f"""Server "{apiname}" was found and updated.""")
            break
    if not found:
        new_server = {"apiurl": apiurl, "apikey": apikey, "name": apiname}
        if directory:
            new_server["directory"] = directory
        if playground:
            new_server["playground"] = playground
        env.append(new_server)
    return env


def save_config(cfg: str, env: list) -> bool:
    try:
        with open(cfg, "w", encoding="utf-8") as fp:
            yaml.dump(env, fp)
        os.chmod(cfg, stat.S_IRUSR | stat.S_IWUSR)
    except Exception as err:
        click.echo(f"Unable to save {cfg} file. {err.__class__.__name__}: {err}")
        return False
    return True


def prompt_for_api(retry: str = False, previous_url: str = None, previous_key: str = None) -> tuple[str, str]:
    if retry:
        if not click.confirm("Do you want to try another URL and API key?", default=True):
            raise click.Abort()
    apiurl = click.prompt(
        """Base URL of your docassemble server (e.g., https://da.example.com)""",
        type=APIURLType(),
        default=previous_url,
    )
    apikey = click.prompt(f"""API key of admin or developer user on {apiurl}""", default=previous_key).strip()
    return apiurl, apikey


def test_apiurl_apikey(apiurl: str, apikey: str) -> bool:
    click.echo("Testing the URL and API key...")
    try:
        api_test = requests.get(apiurl + "/api/package", headers={"X-API-Key": apikey})
        if api_test.status_code != 200:
            if api_test.status_code == 403:
                click.secho(
                    f"""\nThe API KEY is invalid. ({api_test.status_code} {api_test.text.strip()})\n{BELL}""", fg="red"
                )
            else:
                click.secho(
                    f"""\nThe API URL or KEY is invalid. ({api_test.status_code} {api_test.text.strip()})\n{BELL}""",
                    fg="red",
                )
            return False
    except Exception as err:
        click.secho(f"""\n{err.__class__.__name__}""", fg="red")
        click.echo(f"""{err}\n""")
        return False
    click.secho(f"Success!{BELL}", fg="green")
    return True


def add_server_to_env(
    cfg: str = None,
    env: list = None,
    apiurl: str = None,
    apikey: str = None,
    directory: str = None,
    playground: str = None,
):
    if not apiurl or not apikey:
        apiurl, apikey = prompt_for_api()
    while not test_apiurl_apikey(apiurl=apiurl, apikey=apikey):
        apiurl, apikey = prompt_for_api(retry=True, previous_url=apiurl, previous_key=apikey)
    env = add_or_update_env(env=env, apiurl=apiurl, apikey=apikey, directory=directory, playground=playground)
    if cfg:
        if save_config(cfg, env):
            click.echo(f"""Configuration saved: {cfg}""")
    return env


def wait_for_server(playground: bool, task_id: str, apikey: str, apiurl: str, server_version_da: str = "0"):
    def wait_for_server_response(
        playground: bool, task_id: str, apikey: str, apiurl: str, server_version_da: str = "0"
    ):
        tries = 0
        info = {}
        before_wait_for_server = time.time()
        while tries < 300:
            if playground:
                full_url = apiurl + "/api/restart_status"
            else:
                full_url = apiurl + "/api/package_update_status"
            try:
                r = requests.get(full_url, params={"task_id": task_id}, headers={"X-API-Key": apikey}, timeout=600)
            except requests.exceptions.RequestException:
                time.sleep(1)
                tries += 1
                continue
            if r.status_code != 200:
                return "package_update_status returned " + str(r.status_code) + ": " + r.text
            info = r.json()
            if info["status"] == "completed" or info["status"] == "unknown":
                break
            time.sleep(1)
            tries += 1
        after_wait_for_server = time.time()
        success = False
        if playground:
            if info.get("status", None) == "completed":
                success = True
        elif info.get("ok", False):
            success = True
        if not (
            server_version_da == "norestart"
            or packaging_version.parse(server_version_da) >= packaging_version.parse("1.5.3")
        ):
            if DEBUG:
                click.echo(f"""\rPackage install duration: {(after_wait_for_server - before_wait_for_server):.2f}s""")
                click.echo("""\rManually waiting for background processes.""")
            time.sleep(after_wait_for_server - before_wait_for_server)
        if success:
            return True
        click.secho("\nUnable to install package.\n", fg="red")
        if not playground:
            if "error_message" in info and isinstance(info["error_message"], str):
                click.secho(info["error_message"], fg="red")
            else:
                click.echo(info)
        return False

    def format_time(seconds):
        """Format seconds as HH:MM:SS"""
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    click.secho("Waiting for package to install...", fg="cyan")

    result = [None]  # Use list to store result from thread
    exception = [None]  # Use list to store any exception

    def run_wait_for_server():
        try:
            result[0] = wait_for_server_response(
                playground=bool(playground),
                task_id=task_id,
                apikey=apikey,
                apiurl=apiurl,
                server_version_da=server_version_da,
            )
        except Exception as e:
            exception[0] = e

    # Start the installer in a separate thread
    installer_thread = threading.Thread(target=run_wait_for_server)
    installer_thread.daemon = True
    installer_thread.start()

    # Display timer while installer runs
    start_time = time.time()
    while installer_thread.is_alive():
        elapsed = int(time.time() - start_time)
        click.echo(f"""\rElapsed: {format_time(elapsed)}""", nl=False)
        time.sleep(1)

    # Wait for thread to complete
    installer_thread.join()

    click.echo()

    # Re-raise any exception that occurred
    if exception[0]:
        raise exception[0]

    return result[0]


# -----------------------------------------------------------------------------
# package_installer
# -----------------------------------------------------------------------------


def package_installer(directory, apiurl, apikey, playground, restart):
    archive = tempfile.NamedTemporaryFile(suffix=".zip")
    zf = zipfile.ZipFile(archive, compression=zipfile.ZIP_DEFLATED, mode="w")
    try:
        ignore_process = subprocess.run(
            ["git", "ls-files", "-i", "--directory", "-o", "--exclude-standard"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            cwd=directory,
            check=False,
        )
        ignore_process.check_returncode()
        raw_ignore = ignore_process.stdout.splitlines()
    except Exception:
        raw_ignore = []
    to_ignore = [path.rstrip("/") for path in raw_ignore]
    root_directory = None
    has_python_files = False
    this_package_name = None
    dependencies = {}
    for root, dirs, files in os.walk(directory, topdown=True):
        adjusted_root = os.path.relpath(root, directory)
        dirs[:] = [
            d
            for d in dirs
            if d not in EXCLUDED_DIRECTORIES
            and not d.endswith(".egg-info")
            and os.path.normpath(os.path.join(adjusted_root, d)) not in to_ignore
        ]
        if root_directory is None and package_metadata_files_present(root):
            root_directory = root
            this_package_name, dependencies = load_package_metadata(root, files)
        for the_file in files:
            if (
                the_file.endswith("~")
                or the_file.endswith(".pyc")
                or the_file.endswith(".swp")
                or the_file.startswith("#")
                or the_file.startswith(".#")
                or the_file.endswith(".tmp")
                or ".tmp." in the_file
                or the_file.endswith(".swx")
                or (the_file == ".gitignore" and root_directory == root)
                or os.path.normpath(os.path.join(adjusted_root, the_file)) in to_ignore
            ):
                continue
            if (
                not has_python_files
                and the_file.endswith(".py")
                and not (the_file in ("setup.py", "setup.cfg", "pyproject.toml") and root == root_directory)
                and the_file != "__init__.py"
            ):
                has_python_files = True
            zf.write(
                os.path.join(root, the_file),
                os.path.relpath(os.path.join(root, the_file), os.path.join(directory, "..")),
            )
    zf.close()
    archive.seek(0)
    if restart == "no":
        should_restart = False
    elif restart == "yes" or has_python_files:
        should_restart = True
    elif len(dependencies) > 0 or this_package_name:
        try:
            r = requests.get(apiurl + "/api/package", headers={"X-API-Key": apikey}, timeout=600)
        except Exception as err:
            click.secho(f"""\n{err.__class__.__name__}""", fg="red")
            raise click.ClickException(f"""{err}\n""")
        if r.status_code != 200:
            return "/api/package returned " + str(r.status_code) + ": " + r.text
        installed_packages = r.json()
        already_installed = False
        for package_info in installed_packages:
            package_info["alt_name"] = re.sub(r"^docassemble\.", "docassemble-", package_info["name"])
            for dependency_name, dependency_info in dependencies.items():
                if dependency_name in (package_info["name"], package_info["alt_name"]):
                    condition = True
                    if dependency_info["operator"]:
                        if dependency_info["operator"] == "==":
                            condition = packaging_version.parse(package_info["version"]) == packaging_version.parse(
                                dependency_info["version"]
                            )
                        elif dependency_info["operator"] == "<=":
                            condition = packaging_version.parse(package_info["version"]) <= packaging_version.parse(
                                dependency_info["version"]
                            )
                        elif dependency_info["operator"] == ">=":
                            condition = packaging_version.parse(package_info["version"]) >= packaging_version.parse(
                                dependency_info["version"]
                            )
                        elif dependency_info["operator"] == "<":
                            condition = packaging_version.parse(package_info["version"]) < packaging_version.parse(
                                dependency_info["version"]
                            )
                        elif dependency_info["operator"] == ">":  # pragma: no branch
                            condition = packaging_version.parse(package_info["version"]) > packaging_version.parse(
                                dependency_info["version"]
                            )
                    if condition:  # pragma: no branch
                        dependency_info["installed"] = True
            if this_package_name and this_package_name in (package_info["name"], package_info["alt_name"]):
                already_installed = True
        should_restart = bool(
            (not already_installed and len(dependencies) > 0)
            or not all(item["installed"] for item in dependencies.values())
        )
    else:
        should_restart = True
    data = {}
    if should_restart:
        try:
            server_packages = requests.get(apiurl + "/api/package", headers={"X-API-Key": apikey})
            if server_packages.status_code != 200:
                if server_packages.status_code == 403:
                    click.secho("""\nThe API KEY is invalid.""", fg="red")
                server_packages.raise_for_status()
            else:
                installed_packages = server_packages.json()
                for package in installed_packages:
                    if package.get("name", "") == "docassemble.base":
                        server_version_da = package.get("version", "0")
        except Exception as err:
            click.secho(f"""\n{err.__class__.__name__}""", fg="red")
            raise click.ClickException(f"""{err}\n""")
        click.secho("Server will restart.", fg="yellow")
    if not should_restart:
        server_version_da = "norestart"
        data["restart"] = "0"
    if DEBUG:
        click.echo(f"""Server version: {server_version_da}.""")
    if playground:
        if playground != "default":
            data["project"] = playground
        project_endpoint = apiurl + "/api/playground/project"
        project_list = requests.get(project_endpoint, headers={"X-API-Key": apikey})
        if project_list.status_code == 200:
            if playground not in project_list:
                try:
                    requests.post(project_endpoint, data={"project": playground}, headers={"X-API-Key": apikey})
                except Exception:
                    return "create project POST returned " + project_list.text
        else:
            click.echo("\n")
            return (
                "playground list of projects GET returned " + str(project_list.status_code) + ": " + project_list.text
            )
        try:
            r = requests.post(
                apiurl + "/api/playground_install",
                data=data,
                files={"file": archive},
                headers={"X-API-Key": apikey},
                timeout=600,
            )
        except Exception as err:
            click.secho(f"""\n{err.__class__.__name__}""", fg="red")
            raise click.ClickException(f"""{err}\n""")
        if r.status_code == 400:
            try:
                error_message = r.json()
            except Exception:
                error_message = ""
            if "project" not in data or error_message != "Invalid project.":
                return "playground_install POST returned " + str(r.status_code) + ": " + r.text
            try:
                r = requests.post(
                    apiurl + "/api/playground/project",
                    data={"project": data["project"]},
                    headers={"X-API-Key": apikey},
                    timeout=600,
                )
            except Exception as err:
                click.secho(f"""\n{err.__class__.__name__}""", fg="red")
                raise click.ClickException(f"""{err}\n""")
            if r.status_code != 204:
                return (
                    "needed to create playground project but POST to api/playground/project returned "
                    + str(r.status_code)
                    + ": "
                    + r.text
                )
            archive.seek(0)
            try:
                r = requests.post(
                    apiurl + "/api/playground_install",
                    data=data,
                    files={"file": archive},
                    headers={"X-API-Key": apikey},
                    timeout=600,
                )
            except Exception as err:
                click.secho(f"""\n{err.__class__.__name__}""", fg="red")
                raise click.ClickException(f"""{err}\n""")
        if r.status_code == 200:
            try:
                info = r.json()
            except Exception:
                return r.text
            task_id = info["task_id"]
            success = wait_for_server(
                playground=bool(playground),
                task_id=task_id,
                apikey=apikey,
                apiurl=apiurl,
                server_version_da=server_version_da,
            )
        elif r.status_code == 204:
            success = True
        else:
            click.echo("\n")
            return "playground_install POST returned " + str(r.status_code) + ": " + r.text
        if success:
            announce_installed()
        else:
            click.secho(
                f"""\n[{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] Install failed!\n{BELL}""", fg="red"
            )
            return 1
    else:
        try:
            r = requests.post(
                apiurl + "/api/package", data=data, files={"zip": archive}, headers={"X-API-Key": apikey}, timeout=600
            )
        except Exception as err:
            click.secho(f"""\n{err.__class__.__name__}""", fg="red")
            raise click.ClickException(f"""{err}\n""")
        if r.status_code != 200:
            return "package POST returned " + str(r.status_code) + ": " + r.text
        info = r.json()
        task_id = info["task_id"]
        if wait_for_server(
            playground=bool(playground),
            task_id=task_id,
            apikey=apikey,
            apiurl=apiurl,
            server_version_da=server_version_da,
        ):
            announce_installed()
        if not should_restart:
            try:
                r = requests.post(apiurl + "/api/clear_cache", headers={"X-API-Key": apikey}, timeout=600)
            except Exception as err:
                click.secho(f"""\n{err.__class__.__name__}{BELL}""", fg="red")
                raise click.ClickException(f"""{err}\n""")
            if r.status_code != 204:
                return "clear_cache returned " + str(r.status_code) + ": " + r.text
    return 0


# =============================================================================
# install
# =============================================================================


@cli.command(context_settings=CONTEXT_SETTINGS)
@common_params_for_api
@common_params_for_installation
@click.option(
    "--restart",
    "-r",
    type=click.Choice(["yes", "no", "auto"]),
    default="auto",
    show_default=True,
    help="On package install: yes, force a restart | no, do not restart | auto, only restart if the package has any .py files or if there are dependencies to be installed",
)
def install(directory, config, api, server, playground, restart):
    """
    Install a docassemble package on a docassemble server.

    `install` tries to get API info from the --api option first (if used), then from the first server listed in the ~/.docassemblecli file if it exists (unless the --config option is used), then it tries to use environmental variables, and finally it prompts the user directly.
    """
    selected_server = select_server(*config, *api, server)
    click.echo(f"""Server: {selected_server["name"]}""")
    if not playground:
        click.echo("Location: Package")
    else:
        click.echo(f"""Location: Playground "{playground}" """)
    click.secho(f"""[{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] Installing...""", fg="yellow")
    return package_installer(
        directory=directory,
        apiurl=selected_server["apiurl"],
        apikey=selected_server["apikey"],
        playground=playground,
        restart=restart,
    )


@cli.command(context_settings=CONTEXT_SETTINGS)
@common_params_for_api
@common_params_for_runtime_config
@click.option(
    "--playground",
    "-p",
    metavar="(PROJECT)",
    is_flag=False,
    flag_value="default",
    help="Download from the default Playground or from the specified Playground project.",
)
@click.option("--overwrite/--no-overwrite", default=False, show_default=True, help="Overwrite existing files.")
@click.argument("package")
def download(config, api, server, playground, overwrite, package):
    """
    Download a docassemble package from a docassemble server or Playground.
    """
    selected_server = select_server(*config, *api, server)
    package_name = normalize_package_name(package)
    package_file_name = re.sub(r"docassemble\.", "docassemble-", package_name)
    archive = tempfile.NamedTemporaryFile(suffix=".zip")

    try:
        if playground:
            params = {"folder": "packages", "filename": package_name}
            if playground != "default":
                params["project"] = playground
            response = requests.get(
                selected_server["apiurl"] + "/api/playground",
                params=params,
                stream=True,
                timeout=600,
                headers={"X-API-Key": selected_server["apikey"]},
            )
            if response.status_code == 404:
                return "Package not found."
            response.raise_for_status()
        else:
            response = requests.get(
                selected_server["apiurl"] + "/api/package",
                headers={"X-API-Key": selected_server["apikey"]},
                timeout=600,
            )
            if response.status_code != 200:
                return "Unable to connect to server."
            zip_file_number = None
            for item in response.json():
                if item["name"] == package_name:
                    zip_file_number = item.get("zip_file_number")
                    break
            if zip_file_number is None:
                return "Package installed but is not downloadable."
            response = requests.get(
                selected_server["apiurl"] + "/api/file/" + str(zip_file_number),
                stream=True,
                timeout=600,
                headers={"X-API-Key": selected_server["apikey"]},
            )
            response.raise_for_status()
    except requests.exceptions.HTTPError as err:
        return "Error downloading package: " + str(err)
    except Exception as err:
        click.secho(f"""\n{err.__class__.__name__}""", fg="red")
        raise click.ClickException(f"""{err}\n""")

    with open(archive.name, "wb") as fp:
        for chunk in response.iter_content(8192):
            fp.write(chunk)

    with zipfile.ZipFile(archive.name, mode="r") as zf:
        if not overwrite:
            for file_info in zf.infolist():
                if os.path.exists(file_info.filename):
                    return (
                        "Unpacking the package here would overwrite existing files "
                        + f"({file_info.filename}). Use --overwrite if you want to overwrite existing files."
                    )
        zf.extractall(path=os.getcwd())
    click.echo(f"Unpacked {package_file_name}.")
    return 0


@cli.command(context_settings=CONTEXT_SETTINGS)
@common_params_for_api
@common_params_for_runtime_config
@click.option(
    "--restart/--no-restart",
    default=True,
    show_default=True,
    help="Restart the docassemble server after uninstalling the package.",
)
@click.argument("package")
def uninstall(config, api, server, restart, package):
    """
    Uninstall a docassemble package from a docassemble server.
    """
    selected_server = select_server(*config, *api, server)
    package_name = normalize_package_name(package)
    data = {"package": package_name}
    if not restart:
        data["restart"] = "0"

    try:
        response = requests.delete(
            selected_server["apiurl"] + "/api/package",
            params=data,
            headers={"X-API-Key": selected_server["apikey"]},
            timeout=600,
        )
    except Exception as err:
        click.secho(f"""\n{err.__class__.__name__}""", fg="red")
        raise click.ClickException(f"""{err}\n""")
    if response.status_code != 200:
        return "package DELETE returned " + str(response.status_code) + ": " + response.text

    info = response.json()
    if wait_for_server(False, info["task_id"], selected_server["apikey"], selected_server["apiurl"]):
        click.secho(f"""[{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] Uninstalled.{BELL}""", fg="green")
        return 0
    return 1


# -----------------------------------------------------------------------------
# watchdog & hashlib
# -----------------------------------------------------------------------------


def calculate_checksum(filepath: str) -> str:
    hash_md5 = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            while chunk := f.read(4096):
                hash_md5.update(chunk)
    except FileNotFoundError:
        return ""
    except Exception as e:
        click.secho(f"""{e} while calculating checksum.""", fg="red")
        return ""
    return hash_md5.hexdigest()


def scan_directory(directory):
    if DEBUG:
        click.secho("Scanning files...", fg="cyan")
    global FILE_CHECKSUMS
    for current_directory, subdirectories, files in os.walk(directory):
        excluded_directories = EXCLUDED_DIRECTORIES
        subdirectories[:] = [d for d in subdirectories if d not in excluded_directories]
        for file in files:
            filepath = os.path.join(current_directory, file)
            if not matches_ignore_patterns(path=filepath, directory=directory):
                FILE_CHECKSUMS[filepath] = calculate_checksum(filepath)
    if DEBUG:
        click.secho("Scanning complete.", fg="green")


def read_ignore_file(path: str) -> list[str]:
    with open(path, encoding="utf-8") as file:
        return [line.rstrip("\r\n") for line in file]


def load_ignore_patterns(directory: str) -> list[str]:
    gitignore_path = os.path.join(directory, ".gitignore")
    if os.path.exists(gitignore_path):
        ignore_patterns = read_ignore_file(gitignore_path)
    else:
        ignore_patterns = GITIGNORE.split("\n")

    watch_ignore_path = os.path.join(directory, WATCH_IGNORE_FILE)
    if os.path.exists(watch_ignore_path):
        ignore_patterns.extend(read_ignore_file(watch_ignore_path))

    ignore_patterns.extend([".git/", ".gitignore", WATCH_IGNORE_FILE])
    return ignore_patterns


def matches_ignore_patterns(path: str, directory: str) -> bool:
    global WATCH_IGNORE_MTIME, GITIGNORE_MTIME, GITMATCH_COMPILED, GITMATCH_DIRECTORY
    gitignore_path = os.path.join(directory, ".gitignore")
    gitignore_mtime = os.path.getmtime(gitignore_path) if os.path.exists(gitignore_path) else None
    watch_ignore_path = os.path.join(directory, WATCH_IGNORE_FILE)
    watch_ignore_mtime = os.path.getmtime(watch_ignore_path) if os.path.exists(watch_ignore_path) else None
    if (
        not GITMATCH_COMPILED
        or GITMATCH_DIRECTORY != directory
        or GITIGNORE_MTIME != gitignore_mtime
        or WATCH_IGNORE_MTIME != watch_ignore_mtime
    ):
        if DEBUG:
            click.echo("GITMATCH_COMPILED")
        GITMATCH_COMPILED = gitmatch.compile(load_ignore_patterns(directory))
        GITMATCH_DIRECTORY = directory
        GITIGNORE_MTIME = gitignore_mtime
        WATCH_IGNORE_MTIME = watch_ignore_mtime
    # Convert the absolute path to a relative path for gitmatch to work
    path = os.path.relpath(path, directory)
    return GITMATCH_COMPILED.match(path=path)


class WatchHandler(FileSystemEventHandler):
    def __init__(self, *args, **kwargs):
        self.directory = kwargs.pop("directory")
        super(WatchHandler, self).__init__(*args, **kwargs)

    def on_any_event(self, event):
        global LAST_MODIFIED, FILE_CHECKSUMS
        event_type = getattr(event, "event_type", None)
        if event_type in ("opened", "closed") or (event.is_directory and event_type == "modified"):
            return None
        if event.is_directory:
            return None
        event_path = os.path.abspath(event.src_path)
        if matches_ignore_patterns(path=event_path.replace("\\", "/"), directory=self.directory):
            return None
        if event_type in ("created", "modified"):
            new_checksum = calculate_checksum(event_path)
            if not new_checksum or FILE_CHECKSUMS.get(event_path) == new_checksum:
                return None
            FILE_CHECKSUMS[event_path] = new_checksum
        elif event_type == "deleted":
            FILE_CHECKSUMS.pop(event_path, None)
        else:
            return None

        event_bucket = LAST_MODIFIED["files"].setdefault(event_path, {})
        if event_type == "deleted":
            LAST_MODIFIED["files"][event_path] = {"deleted": True}
        else:
            if "deleted" in event_bucket:
                event_bucket = {}
            event_bucket[event_type] = True
            LAST_MODIFIED["files"][event_path] = event_bucket
        LAST_MODIFIED["time"] = time.time()
        if event_path.endswith(".py"):
            LAST_MODIFIED["restart"] = True


# =============================================================================
# watch
# =============================================================================


@cli.command(context_settings=CONTEXT_SETTINGS)
@common_params_for_installation
@common_params_for_api
@click.option(
    "--restart",
    "-r",
    type=click.Choice(["yes", "no", "auto"]),
    default="auto",
    show_default=True,
    help="On package install: yes, force a restart | no, do not restart | auto, only restart if any .py files were changed",
)
@click.option(
    "--buffer",
    "-b",
    metavar="SECONDS",
    default=3,
    show_default=True,
    help="(On server restart only) Set the buffer (wait time) between a file change event and package installation. If you are experiencing multiple installs back-to-back, try increasing this value.",
)
def watch(directory, config, api, server, playground, restart, buffer):
    """
    Watch a package directory and `install` any changes. Press Ctrl + c to exit.

    If the --directory option is not specified, `watch` will look for a directory entry in the config file. The corresponding server entry will be selected automatically if the "directory" key in the config file matches the directory being watched. If a match is found, the "playground" key in the config file will be used if it exists and if no --playground option was specified.
    """
    selected_server = select_server(*config, *api, server, directory=directory)
    restart_param = restart
    scan_directory(directory)
    global FULL_INSTALL_DONE, LAST_MODIFIED
    event_handler = WatchHandler(directory=directory)
    observer = Observer()
    observer.schedule(event_handler, directory, recursive=True)
    observer.start()
    click.echo()
    click.echo(f"""Server: {selected_server["name"]}""")

    if not playground:
        if (
            "directory" in selected_server
            and selected_server["directory"] == directory
            and "playground" in selected_server
        ):
            playground = selected_server["playground"]
        else:
            click.echo("Location: Package")
    if playground:
        click.echo(f"""Location: Playground "{playground}" """)

    if "startup" in selected_server and selected_server["startup"] == "install":
        click.secho("""Installing on startup.""", fg="cyan")
        startup_result = package_installer(
            directory=directory,
            apiurl=selected_server["apiurl"],
            apikey=selected_server["apikey"],
            playground=playground,
            restart=restart,
        )
        FULL_INSTALL_DONE = startup_result == 0
        click.echo("")

    click.echo(f"""Watching: {directory}""")
    click.secho(f"""[{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] Started""", fg="green")
    stop_message = """\nStopping "docassemblecli3 watch"."""
    try:
        while True:
            if LAST_MODIFIED["time"] and time.time() - LAST_MODIFIED["time"] >= WATCH_SETTLE_DELAY:
                changed_files = deduplicate_watch_events(LAST_MODIFIED["files"])
                if not changed_files:
                    LAST_MODIFIED = {"time": 0, "files": {}, "restart": False}
                    time.sleep(0.2)
                    continue
                click.secho(f"""[{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] Installing...""", fg="yellow")
                if restart_param == "yes" or (restart_param == "auto" and LAST_MODIFIED["restart"]):
                    effective_restart = "yes"
                    time.sleep(buffer)
                else:
                    effective_restart = "no"
                for item in changed_files.keys():
                    click.echo("  " + item.replace(directory, ""))
                LAST_MODIFIED = {"time": 0, "files": {}, "restart": False}

                install_result = None
                if playground and FULL_INSTALL_DONE:
                    uploaded = upload_playground_files(
                        apiurl=selected_server["apiurl"],
                        apikey=selected_server["apikey"],
                        playground=playground,
                        changed_files=changed_files,
                    )
                    if uploaded:
                        announce_installed()
                        install_result = 0

                if install_result is None:
                    install_result = package_installer(
                        directory=directory,
                        apiurl=selected_server["apiurl"],
                        apikey=selected_server["apikey"],
                        playground=playground,
                        restart=effective_restart,
                    )
                FULL_INSTALL_DONE = install_result == 0
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        click.echo(f"\nException occurred: {e}")
    finally:
        observer.stop()
        observer.join()
        FULL_INSTALL_DONE = False
    return stop_message


# =============================================================================
# create
# =============================================================================


@cli.command(context_settings=CONTEXT_SETTINGS)
@click.option("--package", metavar="PACKAGE", help="Name of the package you want to create")
@click.option("--developer-name", metavar="NAME", help="Name of the developer of the package")
@click.option("--developer-email", metavar="EMAIL", help="Email of the developer of the package")
@click.option("--description", metavar="DESCRIPTION", help="Description of package")
@click.option("--url", metavar="URL", help="URL of package")
@click.option("--license", metavar="LICENSE", help="License of package")
@click.option("--version", metavar="VERSION", help="Version number of package")
@click.option("--output", metavar="OUTPUT", help="Output directory in which to create the package")
def create(package, developer_name, developer_email, description, url, license, version, output):
    """
    Create an empty docassemble add-on package.
    """
    pkgname = package
    if not pkgname:
        pkgname = click.prompt("Name of the package you want to create (e.g., childsupport)")
    pkgname = re.sub(r"\s", "", pkgname)
    if not pkgname:
        return "The package name you entered is invalid."
    pkgname = re.sub(r"^docassemble[\-\.]", "", pkgname, flags=re.IGNORECASE)
    if output:
        packagedir = output
    else:
        packagedir = "docassemble-" + pkgname
    if os.path.exists(packagedir):
        if not os.path.isdir(packagedir):
            return "Cannot create the directory " + packagedir + " because the path already exists."
        dir_listing = list(os.listdir(packagedir))
        if "setup.py" in dir_listing or "setup.cfg" in dir_listing or "pyproject.toml" in dir_listing:
            return "The directory " + packagedir + " already has a package in it."
    else:
        os.makedirs(packagedir, exist_ok=True)
    if not developer_name:
        developer_name = click.prompt("Name of developer").strip()
        if not developer_name:
            developer_name = "Your Name Here"
    if not developer_email:
        developer_email = click.prompt("Email address of developer (e.g., developer@example.com)").strip()
        if not developer_email:
            developer_email = "developer@example.com"
    if not description:
        description = click.prompt("Description of package (e.g., A docassemble extension)").strip()
        if not description:
            description = "A docassemble extension."
    package_url = url
    if not package_url:
        package_url = click.prompt("URL of package (e.g., https://docassemble.org)").strip()
        if not package_url:
            package_url = "https://docassemble.org"
    if not license:
        license = click.prompt("License of package").strip()
    license = normalize_license_string(license)
    if not version:
        version = click.prompt("Version of package", default="0.0.1", show_default=True).strip()
    initpy = """\
__import__("pkg_resources").declare_namespace(__name__)

"""
    if "MIT" in license:
        licensetext = (
            "The MIT License (MIT)\n\nCopyright (c) "
            + str(datetime.datetime.now().year)
            + " "
            + developer_name
            + """

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
        )
    else:
        licensetext = license + "\n"

    readme = (
        "# docassemble."
        + pkgname
        + "\n\n"
        + description
        + "\n\n## Author\n\n"
        + developer_name
        + ", "
        + developer_email
        + "\n"
    )
    manifestin = (
        """\
include README.md
graft docassemble/"""
        + pkgname
        + """/data
recursive-exclude * *.egg-info
recursive-exclude .git *
recursive-exclude venv *
recursive-exclude .github *
recursive-exclude .pytest_cache *
recursive-exclude .vscode *
recursive-exclude build *
recursive-exclude dist *
recursive-exclude * __pycache__
recursive-exclude * *.pyc
recursive-exclude * *.pyo
recursive-exclude * *.orig
recursive-exclude * *~
recursive-exclude * *.bak
recursive-exclude * *.swp
"""
    )
    setupcfg = """\
[metadata]
description_file = README.md
"""
    pyproject = f"""[build-system]
requires = ["setuptools==80.9.0"]
build-backend = "setuptools.build_meta"

[project]
name = "docassemble.{pkgname}"
version = "{version}"
description = {repr(description)}
readme = "README.md"
requires-python = ">=3.12"
authors = [
    {{ name = {repr(developer_name)}, email = {repr(developer_email)} }},
]
dependencies = []

[project.urls]
Homepage = {repr(package_url)}

[tool.setuptools.packages.find]
where = ["."]
"""
    if license:
        pyproject += f'\nlicense = {repr(license)}\nlicense-files = ["LICENSE"]\n'
    setuppy = """\
import os
import sys
from setuptools import setup, find_packages
from fnmatch import fnmatchcase
from distutils.util import convert_path

standard_exclude = ("*.pyc", "*~", ".*", "*.bak", "*.swp*")
standard_exclude_directories = (".*", "CVS", "_darcs", "./build", "./dist", "EGG-INFO", "*.egg-info")

def find_package_data(where=".", package="", exclude=standard_exclude, exclude_directories=standard_exclude_directories):
    out = {}
    stack = [(convert_path(where), "", package)]
    while stack:
        where, prefix, package = stack.pop(0)
        for name in os.listdir(where):
            fn = os.path.join(where, name)
            if os.path.isdir(fn):
                bad_name = False
                for pattern in exclude_directories:
                    if (fnmatchcase(name, pattern)
                        or fn.lower() == pattern.lower()):
                        bad_name = True
                        break
                if bad_name:
                    continue
                if os.path.isfile(os.path.join(fn, "__init__.py")):
                    if not package:
                        new_package = name
                    else:
                        new_package = package + "." + name
                        stack.append((fn, "", new_package))
                else:
                    stack.append((fn, prefix + name + "/", package))
            else:
                bad_name = False
                for pattern in exclude:
                    if (fnmatchcase(name, pattern)
                        or fn.lower() == pattern.lower()):
                        bad_name = True
                        break
                if bad_name:
                    continue
                out.setdefault(package, []).append(prefix+name)
    return out

"""
    setuppy += (
        "setup(name="
        + repr("docassemble." + pkgname)
        + """,
      version="""
        + repr(version)
        + """,
      description=("""
        + repr(description)
        + """),
      long_description="""
        + repr(readme)
        + """,
      long_description_content_type="text/markdown",
      author="""
        + repr(developer_name)
        + """,
      author_email="""
        + repr(developer_email)
        + """,
      license="""
        + repr(license)
        + """,
      url="""
        + repr(package_url)
        + """,
      packages=find_packages(),
      namespace_packages=["docassemble"],
      install_requires=[],
      zip_safe=False,
      package_data=find_package_data(where='docassemble/"""
        + pkgname
        + """/', package='docassemble."""
        + pkgname
        + """'),
     )
"""
    )
    # maindir = os.path.join(packagedir, "docassemble", pkgname)
    questionsdir = os.path.join(packagedir, "docassemble", pkgname, "data", "questions")
    templatesdir = os.path.join(packagedir, "docassemble", pkgname, "data", "templates")
    staticdir = os.path.join(packagedir, "docassemble", pkgname, "data", "static")
    sourcesdir = os.path.join(packagedir, "docassemble", pkgname, "data", "sources")
    if not os.path.isdir(questionsdir):
        os.makedirs(questionsdir, exist_ok=True)
    if not os.path.isdir(templatesdir):
        os.makedirs(templatesdir, exist_ok=True)
    if not os.path.isdir(staticdir):
        os.makedirs(staticdir, exist_ok=True)
    if not os.path.isdir(sourcesdir):
        os.makedirs(sourcesdir, exist_ok=True)
    with open(os.path.join(packagedir, ".gitignore"), "w", encoding="utf-8") as the_file:
        the_file.write(GITIGNORE)
    with open(os.path.join(packagedir, "README.md"), "w", encoding="utf-8") as the_file:
        the_file.write(readme)
    with open(os.path.join(packagedir, "LICENSE"), "w", encoding="utf-8") as the_file:
        the_file.write(licensetext)
    with open(os.path.join(packagedir, "setup.py"), "w", encoding="utf-8") as the_file:
        the_file.write(setuppy)
    with open(os.path.join(packagedir, "setup.cfg"), "w", encoding="utf-8") as the_file:
        the_file.write(setupcfg)
    with open(os.path.join(packagedir, "MANIFEST.in"), "w", encoding="utf-8") as the_file:
        the_file.write(manifestin)
    with open(os.path.join(packagedir, "pyproject.toml"), "w", encoding="utf-8") as the_file:
        the_file.write(pyproject)
    with open(os.path.join(packagedir, "docassemble", "__init__.py"), "w", encoding="utf-8") as the_file:
        the_file.write(initpy)
    with open(os.path.join(packagedir, "docassemble", pkgname, "__init__.py"), "w", encoding="utf-8") as the_file:
        the_file.write("__version__ = " + repr(version) + "\n")
    return 0


# =============================================================================
# config
# =============================================================================


@config.command(context_settings=CONTEXT_SETTINGS)
@common_params_for_config
@common_params_for_api
@common_params_for_directory_and_playground
def add(config, api, server, directory, playground):
    """
    Add a server to the config file.
    """
    apiurl, apikey = api
    cfg, env = config
    add_server_to_env(cfg=cfg, env=env, apiurl=apiurl, apikey=apikey, directory=directory, playground=playground)


@config.command(context_settings=CONTEXT_SETTINGS)
@common_params_for_config
@click.option("--server", "-s", metavar="SERVER", help="Specify a server to remove from the config file")
def remove(config, server):
    """
    Remove a server from the config file.
    """
    cfg, env = config
    if not server:
        click.echo(f"""Servers in {cfg}:""")
        for item in display_servers(env=env):
            click.echo("  " + item)
        server = click.prompt("Remove which server?")
    selected_server = select_server(cfg=cfg, env=env, server=server)
    env.remove(selected_server)
    save_config(cfg=cfg, env=env)
    click.echo(f"""Server "{server}" has been removed from {cfg}.""")


@config.command(context_settings=CONTEXT_SETTINGS)
@common_params_for_config
def display(config):
    """
    List the servers in the config file.
    """
    _, env = config
    for item in display_servers(env=env):
        click.echo("  " + item)


@config.command(context_settings=CONTEXT_SETTINGS)
@click.argument("config", type=click.File(mode="w", encoding="utf-8"))
def new(config):
    """
    Create a new config file.
    """
    if os.path.exists(config.name) and os.stat(config.name).st_size != 0:
        raise click.BadParameter("File exists and is not empty!")
    env = []
    try:
        yaml.dump(env, config)
        os.chmod(config.name, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        raise click.BadParameter("File is not usable.")
    click.echo(f"""Config created successfully: {os.path.abspath(config.name)}""")
    if click.confirm("Do you want to add a server to this new config file?", default=True):
        apiurl, apikey = prompt_for_api()
        add_server_to_env(cfg=config.name, env=env, apiurl=apiurl, apikey=apikey)


@config.command(context_settings=CONTEXT_SETTINGS, hidden=True)
@common_params_for_config
@common_params_for_api
def server_version(config, api, server):
    selected_server = select_server(*config, *api, server)
    try:
        r = requests.get(
            selected_server["apiurl"] + "/api/package", headers={"X-API-Key": selected_server["apikey"]}, timeout=600
        )
        if DEBUG:
            click.echo(type(r.status_code))
            click.echo(r.status_code)
        if r.status_code != 200:
            if r.status_code == 403:
                click.secho("""\nThe API KEY is invalid.""", fg="red")
            r.raise_for_status()
        installed_packages = r.json()
        for package in installed_packages:
            if package.get("name", "") == "docassemble.base":
                click.echo(package["version"])
    except Exception as err:
        click.secho(f"""\n{err.__class__.__name__}""", fg="red")
        raise click.ClickException(f"""{err}\n""")


@config.command(context_settings=CONTEXT_SETTINGS)
@common_params_for_config
@common_params_for_api
def test(config, api, server):
    """
    Test the URL and API key.
    """
    selected_server = select_server(*config, *api, server)
    apiurl = selected_server["apiurl"]
    apikey = selected_server["apikey"]
    click.echo(apiurl)
    test_apiurl_apikey(apiurl=apiurl, apikey=apikey)
