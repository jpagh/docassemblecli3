import datetime
import gitmatch
import os
import re
import stat
import subprocess
import tempfile
import time
import zipfile
from functools import wraps
from urllib.parse import urlparse

import click
import requests
import yaml
from packaging import version as packaging_version
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

global DEFAULT_CONFIG
DEFAULT_CONFIG = os.path.join(os.path.expanduser("~"), ".docassemblecli")


global LAST_MODIFIED
LAST_MODIFIED = {
    "time": 0,
    "files": {},
    "restart": False,
}

global DEBUG
DEBUG = False


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
.#*
en
*/auto
.history/
.idea
.dir-locals.el
.flake8
*.swp
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


# -----------------------------------------------------------------------------
# click
# -----------------------------------------------------------------------------

CONTEXT_SETTINGS = dict(help_option_names=["--help", "-h"])


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option()
@click.option("--color/--no-color", "-C/-N", default=None, show_default=True, help="Overrides color auto-detection in interactive terminals.")
@click.option("--debug/--no-debug", default=False, hidden=True)
def cli(color, debug):
    """
    Commands for working with docassemble packages and servers.
    """
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
    @click.option("--api", "-a", type=(APIURLType(), str), default=(None, None), help="URL of the docassemble server and API key of the user (admin or developer)")
    @click.option("--server", "-s", metavar="SERVER", default="", help="Specify a server from the config file")
    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper


def common_params_for_config(func):
    @click.option("--config", "-c", default=DEFAULT_CONFIG, type=click.Path(), callback=validate_and_load_or_create_config, show_default=True, help="Specify the config file to use")
    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper


def common_params_for_installation(func):
    @click.option("--directory", "-d", default=os.getcwd(), type=click.Path(), callback=validate_package_directory, help="Specify package directory [default: current directory]")
    @click.option("--config", "-c", is_flag=False, flag_value="", default=DEFAULT_CONFIG, type=click.Path(), callback=validate_and_load_or_create_config, show_default=True, help="Specify the config file to use or leave it blank to skip using any config file")
    @click.option("--playground", "-p", metavar="(PROJECT)", is_flag=False, flag_value="default", help="Install into the default Playground or into the specified Playground project.")
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


def validate_package_directory(ctx, param, directory: str) -> str:
    directory = os.path.abspath(directory)
    if not os.path.exists(directory):
        raise click.BadParameter(f"""Directory "{directory}" does not exist.""")
    if not os.path.isfile(os.path.join(directory, "setup.py")):
        raise click.BadParameter(f"""Directory "{directory}" does not contain a setup.py file, so it is not the directory of a valid Python package.""")
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
    return servers


def select_server(cfg: str = None, env: list = None, apiurl: str = None, apikey: str = None, server: str = "") -> dict:
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
            return env[0]
    if "DOCASSEMBLEAPIURL" in os.environ and "DOCASSEMBLEAPIKEY" in os.environ:
        apiurl: str = os.environ["DOCASSEMBLEAPIURL"]
        apikey: str = os.environ["DOCASSEMBLEAPIKEY"]
        return add_or_update_env(apiurl=apiurl, apikey=apikey)[0]
    return add_server_to_env(cfg, env)[0]


def add_or_update_env(env: list = None, apiurl: str = "", apikey: str = "") -> list:
    if not env:
        env: list = []
    apiname: str = name_from_url(apiurl)
    found: bool = False
    for item in env:
        if item.get("name", None) == apiname:
            item["apiurl"] = apiurl
            item["apikey"] = apikey
            found = True
            click.echo(f"""Server "{apiname}" was found and updated.""")
            break
    if not found:
        env.append({"apiurl": apiurl, "apikey": apikey, "name": apiname})
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
    apiurl = click.prompt("""Base URL of your docassemble server (e.g., https://da.example.com)""", type=APIURLType(), default=previous_url)
    apikey = click.prompt(f"""API key of admin or developer user on {apiurl}""", default=previous_key).strip()
    return apiurl, apikey


def test_apiurl_apikey(apiurl: str, apikey: str) -> bool:
    click.echo("Testing the URL and API key...")
    try:
        api_test = requests.get(apiurl + "/api/package", headers={"X-API-Key": apikey})
        if api_test.status_code != 200:
            if api_test.status_code == 403:
                click.secho(f"""\nThe API KEY is invalid. ({api_test.status_code} {api_test.text.strip()})\n""", fg="red")
            else:
                click.secho(f"""\nThe API URL or KEY is invalid. ({api_test.status_code} {api_test.text.strip()})\n""", fg="red")
            return False
    except Exception as err:
        click.secho(f"""\n{err.__class__.__name__}""", fg="red")
        click.echo(f"""{err}\n""")
        return False
    click.secho("Success!", fg="green")
    return True


def add_server_to_env(cfg: str = None, env: list = None, apiurl: str = None, apikey: str = None):
    if not apiurl or not apikey:
        apiurl, apikey = prompt_for_api()
    while not test_apiurl_apikey(apiurl=apiurl, apikey=apikey):
        apiurl, apikey = prompt_for_api(retry=True, previous_url=apiurl, previous_key=apikey)
    env = add_or_update_env(env=env, apiurl=apiurl, apikey=apikey)
    if cfg:
        if save_config(cfg, env):
            click.echo(f"""Configuration saved: {cfg}""")
    return env


def select_env(cfg: str = None, env: list = None, apiurl: str = None, apikey: str = None, server: str = None) -> dict:
    if apiurl and apikey:
        return add_server_to_env(cfg=cfg, env=env, apiurl=apiurl, apikey=apikey)[-1]
    else:
        return select_server(cfg=cfg, env=env, server=server)


def wait_for_server(playground:bool, task_id: str, apikey: str, apiurl: str, server_version_da: str = "0"):
    click.secho("Waiting for package to install...", fg="cyan")
    tries = 0
    before_wait_for_server = time.time()
    while tries < 300:
        if playground:
            full_url = apiurl + "/api/restart_status"
        else:
            full_url = apiurl + "/api/package_update_status"
        try:
            r = requests.get(full_url, params={"task_id": task_id}, headers={"X-API-Key": apikey}, timeout=600)
        except requests.exceptions.RequestException:
            pass
        if r.status_code != 200:
            return("package_update_status returned " + str(r.status_code) + ": " + r.text)
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
    if not (server_version_da == "norestart" or packaging_version.parse(server_version_da) >= packaging_version.parse("1.5.3")):
        if DEBUG:
            click.echo(f"""Package install duration: {(after_wait_for_server - before_wait_for_server):.2f}s""")
            click.echo("""Manually waiting for background processes.""")
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


# -----------------------------------------------------------------------------
# package_installer
# -----------------------------------------------------------------------------
def package_installer(directory, apiurl, apikey, playground, restart):
    archive = tempfile.NamedTemporaryFile(suffix=".zip")
    zf = zipfile.ZipFile(archive, compression=zipfile.ZIP_DEFLATED, mode="w")
    try:
        ignore_process = subprocess.run(["git", "ls-files", "-i", "--directory", "-o", "--exclude-standard"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, cwd=directory, check=False)
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
        adjusted_root = os.sep.join(root.split(os.sep)[1:])
        dirs[:] = [d for d in dirs if d not in [".git", "__pycache__", ".mypy_cache", ".venv", ".history", "build"] and not d.endswith(".egg-info") and os.path.join(adjusted_root, d) not in to_ignore]
        if root_directory is None and ("setup.py" in files or "setup.cfg" in files):
            root_directory = root
            if "setup.py" in files:
                with open(os.path.join(root, "setup.py"), "r", encoding="utf-8") as fp:
                    setup_text = fp.read()
                    m = re.search(r"""setup\(.*\bname=(["\"])(.*?)(["\"])""", setup_text)
                    if m and m.group(1) == m.group(3):
                        this_package_name = m.group(2).strip()
                    m = re.search(r"""setup\(.*install_requires=\[(.*?)\]""", setup_text, flags=re.DOTALL)
                    if m:
                        for package_text in m.group(1).split(","):
                            package_name = package_text.strip()
                            if len(package_name) >= 3 and package_name[0] == package_name[-1] and package_name[0] in (""", """):
                                package_name = package_name[1:-1]
                                mm = re.search(r"""(.*)(<=|>=|==|<|>)(.*)""", package_name)
                                if mm:
                                    dependencies[mm.group(1).strip()] = {"installed": False, "operator": mm.group(2), "version": mm.group(3).strip()}
                                else:
                                    dependencies[package_name] = {"installed": False, "operator": None, "version": None}
        for the_file in files:
            if the_file.endswith("~") or the_file.endswith(".pyc") or the_file.endswith(".swp") or the_file.startswith("#") or the_file.startswith(".#") or (the_file == ".gitignore" and root_directory == root) or os.path.join(adjusted_root, the_file) in to_ignore:
                continue
            if not has_python_files and the_file.endswith(".py") and not (the_file == "setup.py" and root == root_directory) and the_file != "__init__.py":
                has_python_files = True
            zf.write(os.path.join(root, the_file), os.path.relpath(os.path.join(root, the_file), os.path.join(directory, "..")))
    zf.close()
    archive.seek(0)
    if restart == "no":
        should_restart = False
    elif restart =="yes" or has_python_files:
        should_restart = True
    elif len(dependencies) > 0 or this_package_name:
        try:
            r = requests.get(apiurl + "/api/package", headers={"X-API-Key": apikey}, timeout=600)
        except Exception as err:
            click.secho(f"""\n{err.__class__.__name__}""", fg="red")
            raise click.ClickException(f"""{err}\n""")
        if r.status_code != 200:
            return("/api/package returned " + str(r.status_code) + ": " + r.text)
        installed_packages = r.json()
        already_installed = False
        for package_info in installed_packages:
            package_info["alt_name"] = re.sub(r"^docassemble\.", "docassemble-", package_info["name"])
            for dependency_name, dependency_info in dependencies.items():
                if dependency_name in (package_info["name"], package_info["alt_name"]):
                    condition = True
                    if dependency_info["operator"]:
                        if dependency_info["operator"] == "==":
                            condition = packaging_version.parse(package_info["version"]) == packaging_version.parse(dependency_info["version"])
                        elif dependency_info["operator"] == "<=":
                            condition = packaging_version.parse(package_info["version"]) <= packaging_version.parse(dependency_info["version"])
                        elif dependency_info["operator"] == ">=":
                            condition = packaging_version.parse(package_info["version"]) >= packaging_version.parse(dependency_info["version"])
                        elif dependency_info["operator"] == "<":
                            condition = packaging_version.parse(package_info["version"]) < packaging_version.parse(dependency_info["version"])
                        elif dependency_info["operator"] == ">":
                            condition = packaging_version.parse(package_info["version"]) > packaging_version.parse(dependency_info["version"])
                    if condition:
                        dependency_info["installed"] = True
            if this_package_name and this_package_name in (package_info["name"], package_info["alt_name"]):
                already_installed = True
        should_restart = bool((not already_installed and len(dependencies) > 0) or not all(item["installed"] for item in dependencies.values()))
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
                    if package.get("name", "") == "docassemble":
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
                    return("create project POST returned " + project_list.text)
        else:
            click.echo("\n")
            return("playground list of projects GET returned " + str(project_list.status_code) + ": " + project_list.text)
        try:
            r = requests.post(apiurl + "/api/playground_install", data=data, files={"file": archive}, headers={"X-API-Key": apikey}, timeout=600)
        except Exception as err:
            click.secho(f"""\n{err.__class__.__name__}""", fg="red")
            raise click.ClickException(f"""{err}\n""")
        if r.status_code == 400:
            try:
                error_message = r.json()
            except Exception:
                error_message = ""
            if "project" not in data or error_message != "Invalid project.":
                return("playground_install POST returned " + str(r.status_code) + ": " + r.text)
            try:
                r = requests.post(apiurl + "/api/playground/project", data={"project": data["project"]}, headers={"X-API-Key": apikey}, timeout=600)
            except Exception as err:
                click.secho(f"""\n{err.__class__.__name__}""", fg="red")
                raise click.ClickException(f"""{err}\n""")
            if r.status_code != 204:
                return("needed to create playground project but POST to api/playground/project returned " + str(r.status_code) + ": " + r.text)
            archive.seek(0)
            try:
                r = requests.post(apiurl + "/api/playground_install", data=data, files={"file": archive}, headers={"X-API-Key": apikey}, timeout=600)
            except Exception as err:
                click.secho(f"""\n{err.__class__.__name__}""", fg="red")
                raise click.ClickException(f"""{err}\n""")
        if r.status_code == 200:
            try:
                info = r.json()
            except Exception:
                return(r.text)
            task_id = info["task_id"]
            success = wait_for_server(playground=bool(playground), task_id=task_id, apikey=apikey, apiurl=apiurl, server_version_da=server_version_da)
        elif r.status_code == 204:
            success = True
        else:
            click.echo("\n")
            return("playground_install POST returned " + str(r.status_code) + ": " + r.text)
        if success:
            click.secho(f"""[{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] Installed.""", fg="green")
        else:
            click.secho(f"""\n[{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] Install failed!\n""", fg="red")
            return 1
    else:
        try:
            r = requests.post(apiurl + "/api/package", data=data, files={"zip": archive}, headers={"X-API-Key": apikey}, timeout=600)
        except Exception as err:
            click.secho(f"""\n{err.__class__.__name__}""", fg="red")
            raise click.ClickException(f"""{err}\n""")
        if r.status_code != 200:
            return("package POST returned " + str(r.status_code) + ": " + r.text)
        info = r.json()
        task_id = info["task_id"]
        if wait_for_server(playground=bool(playground), task_id=task_id, apikey=apikey, apiurl=apiurl, server_version_da=server_version_da):
            click.secho(f"""[{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] Installed.""", fg="green")
        if not should_restart:
            try:
                r = requests.post(apiurl + "/api/clear_cache", headers={"X-API-Key": apikey}, timeout=600)
            except Exception as err:
                click.secho(f"""\n{err.__class__.__name__}""", fg="red")
                raise click.ClickException(f"""{err}\n""")
            if r.status_code != 204:
                return("clear_cache returned " + str(r.status_code) + ": " + r.text)
    return 0


# =============================================================================
# install
# =============================================================================
@cli.command(context_settings=CONTEXT_SETTINGS)
@common_params_for_api
@common_params_for_installation
@click.option("--restart", "-r", type=click.Choice(["yes", "no", "auto"]), default="auto", show_default=True, help="On package install: yes, force a restart | no, do not restart | auto, only restart if the package has any .py files or if there are dependencies to be installed")
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
    package_installer(directory=directory, apiurl=selected_server["apiurl"], apikey=selected_server["apikey"], playground=playground, restart=restart)
    return 0


# -----------------------------------------------------------------------------
# watchdog
# -----------------------------------------------------------------------------

def matches_ignore_patterns(path: str, directory: str) -> bool:
    if os.path.exists(gitignore_path := os.path.join(directory, ".gitignore")):
        with open(gitignore_path) as file:
            ignore_patterns = [line.strip() for line in file]
    else:
        ignore_patterns = GITIGNORE.split("\n")
    ignore_patterns.extend([".git/", ".gitignore"])
    gm = gitmatch.compile(ignore_patterns)
    # Convert the absolute path to a relative path for gitmatch to work
    path = os.path.relpath(path, directory)
    return gm.match(path=path)


class WatchHandler(FileSystemEventHandler):
    def __init__(self, *args, **kwargs):
        self.directory = kwargs.pop("directory")
        super(WatchHandler, self).__init__(*args, **kwargs)

    def on_any_event(self, event):
        global LAST_MODIFIED
        if event.is_directory:
            return None
        if event.event_type == "created" or event.event_type == "modified":
            path = event.src_path.replace("\\", "/")
            if not matches_ignore_patterns(path=path, directory=self.directory):
                LAST_MODIFIED["time"] = time.time()
                LAST_MODIFIED["files"][str(event.src_path)] = True
                if str(event.src_path).endswith(".py"):
                    LAST_MODIFIED["restart"] = True


# =============================================================================
# watch
# =============================================================================
@cli.command(context_settings=CONTEXT_SETTINGS)
@common_params_for_installation
@common_params_for_api
@click.option("--restart", "-r", type=click.Choice(["yes", "no", "auto"]), default="auto", show_default=True, help="On package install: yes, force a restart | no, do not restart | auto, only restart if any .py files were changed")
@click.option("--buffer", "-b", metavar="SECONDS", default=3, show_default=True, help="(On server restart only) Set the buffer (wait time) between a file change event and package installation. If you are experiencing multiple installs back-to-back, try increasing this value.")
def watch(directory, config, api, server, playground, restart, buffer):
    """
    Watch a package directory and `install` any changes. Press Ctrl + c to exit.
    """
    selected_server = select_server(*config, *api, server)
    restart_param = restart
    global LAST_MODIFIED
    event_handler = WatchHandler(directory=directory)
    observer = Observer()
    observer.schedule(event_handler, directory, recursive=True)
    observer.start()
    click.echo()
    click.echo(f"""Server: {selected_server["name"]}""")
    if not playground:
        click.echo("Location: Package")
    else:
        click.echo(f"""Location: Playground "{playground}" """)
    click.echo(f"""Watching: {directory}""")
    click.secho(f"""[{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] Started""", fg="green")
    try:
        while True:
            if LAST_MODIFIED["time"]:
                click.secho(f"""[{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] Installing...""", fg="yellow")
                if restart_param == "yes" or (restart_param == "auto" and LAST_MODIFIED["restart"]):
                    restart = "yes"
                    time.sleep(buffer)
                else:
                    restart = "no"
                for item in LAST_MODIFIED["files"].keys():
                    click.echo("  " + item.replace(directory, ""))
                LAST_MODIFIED["time"] = 0
                LAST_MODIFIED["files"] = {}
                LAST_MODIFIED["restart"] = False
                package_installer(directory=directory, apiurl=selected_server["apiurl"], apikey=selected_server["apikey"], playground=playground, restart=restart)
                # click.echo(f"""\n[{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] Watching... {directory}""")
            time.sleep(1)
    except Exception as e:
        click.echo(f"\nException occurred: {e}")
    finally:
        observer.stop()
        observer.join()
        return("""\nStopping "docassemblecli3 watch".""")


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
        return("The package name you entered is invalid.")
    pkgname = re.sub(r"^docassemble[\-\.]", "", pkgname, flags=re.IGNORECASE)
    if output:
        packagedir = output
    else:
        packagedir = "docassemble-" + pkgname
    if os.path.exists(packagedir):
        if not os.path.isdir(packagedir):
            return("Cannot create the directory " + packagedir + " because the path already exists.")
        dir_listing = list(os.listdir(packagedir))
        if "setup.py" in dir_listing or "setup.cfg" in dir_listing:
            return("The directory " + packagedir + " already has a package in it.")
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
        license = click.prompt("License of package", default="MIT", show_default=True).strip()
    if not version:
        version = click.prompt("Version of package", default="0.0.1", show_default=True).strip()
    initpy = """\
__import__("pkg_resources").declare_namespace(__name__)

"""
    if "MIT" in license:
        licensetext = "The MIT License (MIT)\n\nCopyright (c) " + str(datetime.datetime.now().year) + " " + developer_name + """

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
    else:
        licensetext = license + "\n"

    readme = "# docassemble." + pkgname + "\n\n" + description + "\n\n## Author\n\n" + developer_name + ", " + developer_email + "\n"
    manifestin = """\
include README.md
"""
    setupcfg = """\
[metadata]
description_file = README.md
"""
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
    setuppy += "setup(name=" + repr("docassemble." + pkgname) + """,
      version=""" + repr(version) + """,
      description=(""" + repr(description) + """),
      long_description=""" + repr(readme) + """,
      long_description_content_type="text/markdown",
      author=""" + repr(developer_name) + """,
      author_email=""" + repr(developer_email) + """,
      license=""" + repr(license) + """,
      url=""" + repr(package_url) + """,
      packages=find_packages(),
      namespace_packages=["docassemble"],
      install_requires=[],
      zip_safe=False,
      package_data=find_package_data(where='docassemble/""" + pkgname + """/', package='docassemble.""" + pkgname + """'),
     )
"""
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
    with open(os.path.join(packagedir, '.gitignore'), 'w', encoding='utf-8') as the_file:
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
@click.option("--api", "-a", type=(APIURLType(), str), default=(None, None), help="URL of the docassemble server and API key of the user (admin or developer)")
def add(config, api):
    """
    Add a server to the config file.
    """
    apiurl, apikey = api
    if not apiurl or not apikey:
        apiurl, apikey = prompt_for_api(previous_url=apiurl, previous_key=apikey)
    cfg, env = config
    add_server_to_env(cfg=cfg, env=env, apiurl=apiurl, apikey=apikey)


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
        r = requests.get(selected_server["apiurl"] + "/api/package", headers={"X-API-Key": selected_server["apikey"]}, timeout=600)
        if DEBUG:
            click.echo(type(r.status_code))
        if r.status_code != 200:
            if r.status_code == 403:
                click.secho("""\nThe API KEY is invalid.""", fg="red")
            r.raise_for_status()
        installed_packages = r.json()
        for package in installed_packages:
            if package.get("name", "") == "docassemble":
                click.echo(package["version"])
    except Exception as err:
        click.secho(f"""\n{err.__class__.__name__}""", fg="red")
        raise click.ClickException(f"""{err}\n""")


@config.command(context_settings=CONTEXT_SETTINGS, hidden=True)
@common_params_for_config
@common_params_for_api
def test(config, api, server):
    selected_server = select_server(*config, *api, server)
    apiurl = selected_server["apiurl"]
    apikey = selected_server["apikey"]
    test_apiurl_apikey(apiurl=apiurl, apikey=apikey)


