import configparser
import datetime
import io
import math
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
import tomllib
import zipfile
from dataclasses import dataclass
from functools import wraps
from urllib.parse import urlparse

import click
import gitmatch
import niquests as requests
import xxhash
import yaml
from packaging import version as packaging_version
from packaging.licenses import LICENSES as SPDX_LICENSES
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

DEFAULT_CONFIG = os.path.join(os.path.expanduser("~"), ".docassemblecli")
PROJECT_CONFIG = ".docassemblecli"
LAST_MODIFIED = {
    "time": 0,
    "files": {},
    "restart": False,
}
LAST_MODIFIED_LOCK = threading.Lock()
WATCHED_FILES: dict[str, "WatchState"] = {}
RETRY_QUEUE: dict[str, tuple[float, float]] = {}
PENDING_DELETIONS: dict[str, tuple[float, float]] = {}
UPLOADED_NAMES: dict[tuple[str, str], set[str]] = {}
CHUNK_SIZE = 4 * 1024 * 1024
DEBUG = False
BELL = "\a"
WATCH_SWEEP_INTERVAL = 300.0
RETRY_BACKOFF_CAP = 60.0
MAX_ARCHIVE_SKIPS = 3
MIN_SWEEP_INTERVAL = 1.0
EXCLUDED_DIRECTORIES = [".git", "__pycache__", ".mypy_cache", ".venv", ".history", "build"]
WATCH_IGNORE_MTIME = None
WATCH_SETTLE_DELAY = 0.6
WATCH_IGNORE_FILE = ".dawatchignore"
GITMATCH_COMPILED = None
GITMATCH_DIRECTORY = None
GITIGNORE_MTIME = None
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


class DaCliError(Exception):
    """An error that can be shown to the user without a traceback."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class ServerStatusError(DaCliError):
    """The server reported that an install/restart task failed."""


@dataclass
class WatchState:
    """Per-file state maintained by the watch loop.

    `uploaded_hash` is only ever set to a checksum the server confirmed it
    received, so a file is dirty (needs upload) exactly when its current
    checksum differs from `uploaded_hash` (or nothing was uploaded yet).
    `checksum` is the last content hash computed; the file is re-hashed on
    every check, so a change is always detected regardless of mtime/size
    resolution.

    `skip_count` counts consecutive install cycles in which the file was in
    the upload batch but could not be archived; once it reaches
    MAX_ARCHIVE_SKIPS the file is `suspended` — it is left out of upload
    batches until its content changes again, so a file that never stops
    changing can not keep triggering full installs. `suspended_checksum`
    is the content at suspension time, used to detect the next change.
    """

    checksum: str
    uploaded_hash: str | None = None
    skip_count: int = 0
    suspended: bool = False
    suspended_checksum: str | None = None


# -----------------------------------------------------------------------------
# click
# -----------------------------------------------------------------------------


CONTEXT_SETTINGS = {"help_option_names": ["--help", "-h"]}


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
        "--project-config/--no-project-config",
        default=True,
        show_default=True,
        help="Use .docassemblecli from the package directory first, then fall back to the selected config file",
    )
    @click.option(
        "--playground",
        "-p",
        metavar="(PROJECT)",
        is_flag=False,
        flag_value="default",
        help="Install into the default Playground or into the specified Playground project.",
    )
    @click.option(
        "--no-playground",
        is_flag=True,
        help="Install as a package, ignoring any Playground setting in the config.",
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
    @click.option(
        "--no-playground",
        is_flag=True,
        help="Install as a package, ignoring any Playground setting in the config.",
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
    click.secho(
        f"[{datetime.datetime.now(tz=datetime.UTC).strftime('%Y-%m-%d %H:%M:%S')}] Installed.{BELL}", fg="green"
    )


def format_install_location(playground: str | None) -> str:
    if not playground:
        return "Package"
    return f'''Playground "{playground}"'''


def show_dry_run_file_hint(show_files: bool) -> None:
    if not show_files:
        click.echo("Use --show-files to list the files in the preview.")


def show_dry_run_package_install(
    playground: str | None, should_restart: bool, archived_files: list[str], show_files: bool = False
) -> None:
    click.secho("Dry run: no changes were sent.", fg="cyan")
    click.echo(f"Would upload {len(archived_files)} file(s) to {format_install_location(playground)}.")
    click.echo(f"Would restart server: {'yes' if should_restart else 'no'}")
    if show_files and archived_files:
        click.echo("Files to upload:")
        for archived_file in archived_files:
            click.echo("  " + archived_file)
    elif archived_files:
        show_dry_run_file_hint(show_files)


def show_dry_run_playground_upload(playground: str, uploads: dict[str, list[str]], show_files: bool = False) -> None:
    total_files = sum(len(files_to_upload) for files_to_upload in uploads.values())
    click.secho("Dry run: no changes were sent.", fg="cyan")
    click.echo(f"Would upload {total_files} changed file(s) to {format_install_location(playground)}.")
    click.echo(f"Would restart server: {'yes' if uploads['modules'] else 'no'}")
    click.echo("Folders to upload:")
    for folder in ("questions", "sources", "static", "templates", "modules"):
        if uploads[folder]:
            click.echo(f"  {folder}: {len(uploads[folder])}")
    if show_files:
        click.echo("Files to upload:")
        for folder in ("questions", "sources", "static", "templates", "modules"):
            for file_path in uploads[folder]:
                click.echo("  " + file_path)
    elif total_files:
        show_dry_run_file_hint(show_files)


def playground_project_exists(project_data, playground: str) -> bool:
    if isinstance(project_data, list):
        for item in project_data:
            if item == playground:
                return True
            if isinstance(item, dict) and playground in (item.get("name"), item.get("project")):
                return True
        return False
    if isinstance(project_data, dict):
        for key in ("projects", "items", "results"):
            if key in project_data:
                return playground_project_exists(project_data[key], playground)
    return False


def http_request(method_name: str, url: str, **kwargs):
    request_method = getattr(requests, method_name)
    if getattr(request_method, "__module__", "").startswith("niquests"):
        with requests.Session(disable_http3=True) as session:
            return getattr(session, method_name)(url, **kwargs)
    return request_method(url, **kwargs)


def http_get(url: str, **kwargs):
    return http_request("get", url, **kwargs)


def http_post(url: str, **kwargs):
    return http_request("post", url, **kwargs)


def http_delete(url: str, **kwargs):
    return http_request("delete", url, **kwargs)


def refresh_path_state(path: str) -> bool:
    """Hash `path` and update its WatchState; return True if it is dirty.

    The file is hashed on every call — no mtime/size gate — and it is dirty
    when its content checksum differs from the last checksum the server
    confirmed (`uploaded_hash`), or when it has never been uploaded.
    """
    checksum = calculate_checksum(path)
    if not checksum:
        return False
    state = WATCHED_FILES.get(path)
    if state is None:
        WATCHED_FILES[path] = WatchState(checksum, None)
        return True
    state.checksum = checksum
    if state.suspended:
        if checksum == state.suspended_checksum:
            return False
        state.suspended = False
        state.suspended_checksum = None
    return state.uploaded_hash is None or checksum != state.uploaded_hash


def mark_uploaded(path: str, sent_hash: str) -> None:
    """Record that the server confirmed `sent_hash` for `path`.

    The file is re-hashed to guard against a change made while it was being
    uploaded; if the content no longer matches what the server received, the
    file stays dirty so the next cycle re-uploads it.
    """
    state = WATCHED_FILES.get(path)
    if state is None:
        return
    checksum = calculate_checksum(path)
    if not checksum:
        return
    state.checksum = checksum
    if checksum == sent_hash:
        state.uploaded_hash = sent_hash


def handle_watch_events(events: dict[str, dict]) -> tuple[list[str], list[str]]:
    """Apply buffered watchdog events to WATCHED_FILES.

    Returns (dirty_paths, deleted_paths). Only paths whose content actually
    differs from what the server has are reported as dirty; mtime-only
    touches are deduplicated here.
    """
    dirty = []
    deleted = []
    for path, bucket in events.items():
        if bucket.get("deleted"):
            was_tracked = path in WATCHED_FILES or path in RETRY_QUEUE
            WATCHED_FILES.pop(path, None)
            RETRY_QUEUE.pop(path, None)
            if was_tracked:
                deleted.append(path)
            continue
        if refresh_path_state(path):
            dirty.append(path)
    return dirty, deleted


def backoff_paths(paths: list[str], retry_map: dict[str, tuple[float, float]], delay: float = 1.0) -> None:
    """Schedule retries for failed uploads/deletions with exponential backoff."""
    for path in paths:
        existing = retry_map.get(path)
        new_delay = min(existing[1] * 2, RETRY_BACKOFF_CAP) if existing else delay
        retry_map[path] = (time.monotonic() + new_delay, new_delay)


def mark_previewed(paths: list[str]) -> None:
    """In dry-run mode, remember previewed content so previews dedupe."""
    for path in paths:
        state = WATCHED_FILES.get(path)
        if state is not None:
            state.uploaded_hash = state.checksum


def mark_archive_uploaded(archive_map: dict[str, str], directory: str, playground: str | None) -> None:
    """Mark tracked files as uploaded based on what a successful install sent.

    The file is re-hashed and compared against the checksum the archive
    contained; files that changed during the install keep their (dirty)
    state so the next cycle re-uploads them. In Playground mode the ledger of
    CLI-uploaded server names is populated for reconciliation.
    """
    if not archive_map:
        return
    archive_root = os.path.join(os.path.abspath(directory), "..")
    for path in list(WATCHED_FILES):
        relpath = os.path.relpath(path, archive_root)
        archived = archive_map.get(relpath)
        if archived is None:
            continue
        state = WATCHED_FILES.get(path)
        if state is None:
            continue
        checksum = calculate_checksum(path)
        if not checksum:
            continue
        state.checksum = checksum
        if checksum == archived:
            state.uploaded_hash = checksum
            state.suspended = False
            state.suspended_checksum = None
        if playground:
            folder = playground_folder_of(path)
            if folder:
                project = playground if playground != "default" else "default"
                UPLOADED_NAMES.setdefault((folder, project), set()).add(server_file_name(path))


def schedule_unconfirmed_retries(dirty_paths: list[str], archive_map: dict, directory: str) -> None:
    """Retry the batch files a successful install could not confirm.

    Files that were never archived (unreadable, or changing while the archive
    was built) are scheduled for a backoff retry so they can not silently stay
    stale on the server. Files that keep failing to archive are suspended
    after MAX_ARCHIVE_SKIPS consecutive attempts and re-attempted only when
    their content changes again, so a file that never stops changing can not
    keep triggering full installs. Files that were archived but changed during
    the install are left to their own events.
    """
    archive_root = os.path.join(os.path.abspath(directory), "..")
    unconfirmed = []
    for path in dirty_paths:
        state = WATCHED_FILES.get(path)
        if state is None:
            continue
        if state.uploaded_hash == state.checksum:
            state.skip_count = 0
            state.suspended = False
            continue
        if os.path.relpath(path, archive_root) in archive_map:
            continue
        unconfirmed.append(path)
    retry = []
    for path in unconfirmed:
        state = WATCHED_FILES.get(path)
        state.skip_count += 1
        if state.skip_count >= MAX_ARCHIVE_SKIPS:
            state.suspended = True
            state.suspended_checksum = state.checksum
            state.skip_count = 0
            RETRY_QUEUE.pop(path, None)
            click.secho(
                f"{path} could not be archived after {MAX_ARCHIVE_SKIPS} attempts and will be skipped until it changes.",
                fg="red",
            )
        else:
            retry.append(path)
    backoff_paths(retry, RETRY_QUEUE)


def playground_folder_of(file_path: str) -> str | None:
    """Return the Playground folder a local file belongs to, or None."""
    normalized_path = "/".join(os.path.normpath(file_path).split(os.sep))
    match = re.search(r"/docassemble/([^/]+)/data/([^/]+)/", normalized_path)
    if match and match.group(2) in ("questions", "sources", "static", "templates", "modules"):
        return match.group(2)
    match = re.search(r"/docassemble/([^/]+)/([^/]+)\.py$", normalized_path)
    if match:
        return "modules"
    return None


def classify_playground_paths(paths: list[str]) -> dict[str, list[str]] | None:
    uploads = {"questions": [], "sources": [], "static": [], "templates": [], "modules": []}
    for file_path in paths:
        folder = playground_folder_of(file_path)
        if folder is None:
            return None
        uploads[folder].append(file_path)
    return uploads


def server_file_name(file_path: str) -> str:
    return os.path.basename(file_path)


def playground_name_conflicts(paths: list[str]) -> list[tuple[str, str, str]]:
    """Return (path_a, path_b, folder) for pairs of files that would map to the
    same Playground file, because the Playground stores files flat by name.

    Uploading either file would silently overwrite the other on the server, so
    callers skip the conflicting files in Playground sync (package installs
    are unaffected: they preserve the directory structure).
    """
    by_name: dict[tuple[str, str], list[str]] = {}
    for path in paths:
        folder = playground_folder_of(path)
        if folder is None:
            continue
        by_name.setdefault((folder, server_file_name(path)), []).append(path)
    conflicts = []
    for (folder, _name), group in by_name.items():
        if len(group) > 1:
            # every member of the group conflicts with every other, so all
            # pairs are reported: the skip set and the warning must cover
            # every same-named file, not just the first two
            for i, path_a in enumerate(group):
                for path_b in group[i + 1 :]:
                    conflicts.append((path_a, path_b, folder))
    return conflicts


def playground_conflict_paths(paths: list[str]) -> set[str]:
    """Return every path that participates in a Playground name conflict."""
    return {path for pair in playground_name_conflicts(paths) for path in pair[:2]}


def _readable_paths(paths: list[str]) -> list[str]:
    """Return the paths that are currently readable.

    A file that can not be read can never reach the server, so it must not
    count as a Playground conflict participant: it would only keep its
    readable same-named sibling from syncing. The package installer filters
    its conflict candidates through this too, so both sync paths agree on
    what counts as a conflict.
    """
    return [path for path in paths if os.access(path, os.R_OK)]


def format_playground_conflict_skip(conflicts: list[tuple[str, str, str]]) -> str:
    lines = [
        (
            "Playground name conflict: the Playground stores files flat by name, so the "
            "following files would overwrite each other on the server. They will not be "
            "synced to the Playground (package installs still include them):"
        )
    ]
    by_name: dict[tuple[str, str], set[str]] = {}
    for path_a, path_b, folder in conflicts:
        by_name.setdefault((folder, server_file_name(path_a)), set()).update((path_a, path_b))
    for (folder, name), paths in sorted(by_name.items()):
        quoted = ", ".join(f'"{path}"' for path in sorted(paths))
        quantifier = "both" if len(paths) == 2 else "all"
        lines.append(f'  {quoted} {quantifier} map to "{name}" in folder "{folder}"')
    lines.append("Rename or remove one of each pair to sync them to the Playground.")
    return "\n".join(lines)


def playground_upload_batch(
    apiurl: str, apikey: str, playground: str, dirty_paths: list[str]
) -> tuple[dict[str, str], list[str]]:
    """Upload dirty files to the Playground, one request per folder.

    Returns (sent_hashes, failed_paths). `sent_hashes` maps each uploaded
    path to the checksum of the exact bytes that were sent, so the caller can
    mark the file as uploaded without racing a file that changed mid-upload.
    Errors are printed and collected, never raised.
    """
    uploads = classify_playground_paths(dirty_paths)
    if uploads is None:
        raise DaCliError("changed files are not all in Playground locations")
    project = playground if playground and playground != "default" else None
    sent_hashes: dict[str, str] = {}
    failed: list[str] = []
    for folder in ("questions", "sources", "static", "templates", "modules"):
        files_to_upload = uploads[folder]
        if not files_to_upload:
            continue
        contents = {}
        missing = []
        for file_path in files_to_upload:
            try:
                with open(file_path, "rb") as fp:
                    data = fp.read()
            except OSError:
                missing.append(file_path)
                continue
            contents[file_path] = (server_file_name(file_path), data)
        for file_path in missing:
            failed.append(file_path)
            click.secho(f"\nFile not found: {file_path}", fg="red")
        if not contents:
            continue
        post_data = {"folder": folder, "restart": "1" if folder == "modules" else "0"}
        if project:
            post_data["project"] = project
        files_param = [("files[]", (name, io.BytesIO(data))) for name, data in contents.values()]
        try:
            response = http_post(
                apiurl + "/api/playground",
                data=post_data,
                files=files_param,
                headers={"X-API-Key": apikey},
                timeout=600,
            )
        except requests.exceptions.RequestException as err:
            failed.extend(files_to_upload)
            click.secho(f"\n{err.__class__.__name__}: {err}", fg="red")
            continue
        try:
            if response.status_code == 200:
                try:
                    info = response.json()
                except requests.exceptions.JSONDecodeError:
                    raise DaCliError("server returned invalid JSON: " + response.text)
                if not isinstance(info, dict):
                    raise DaCliError("server returned non-object JSON: " + str(info))
                task_id = info.get("task_id")
                if task_id is None:
                    raise DaCliError("server response missing task_id: " + str(info))
                wait_for_server(True, task_id, apikey, apiurl)
            elif response.status_code != 204:
                raise DaCliError(f"playground upload ({folder}) returned {response.status_code}: {response.text}")
        except DaCliError as err:
            failed.extend(files_to_upload)
            click.secho(f"\n{err}", fg="red")
            continue
        for file_path, (name, data) in contents.items():
            sent_hashes[file_path] = xxhash.xxh64(data).hexdigest()
            UPLOADED_NAMES.setdefault((folder, project or "default"), set()).add(name)
    return sent_hashes, failed


def playground_delete_server_file(
    apiurl: str, apikey: str, folder: str, filename: str, project: str | None = None
) -> None:
    """Delete a file from the Playground.

    Deleting a nonexistent file succeeds (the endpoint is idempotent); a 404
    is treated as success too, because some servers (or proxies in front of
    them) report a missing file that way, and the file is already gone, which
    is the state a delete is meant to produce. Raises DaCliError on failure;
    on success the name is removed from UPLOADED_NAMES.
    """
    params = {"folder": folder, "filename": filename, "restart": "1" if folder == "modules" else "0"}
    if project:
        params["project"] = project
    try:
        response = http_delete(apiurl + "/api/playground", params=params, headers={"X-API-Key": apikey}, timeout=600)
    except requests.exceptions.RequestException as err:
        raise DaCliError(f"{err.__class__.__name__}: {err}") from err
    if response.status_code == 200:
        try:
            info = response.json()
        except requests.exceptions.JSONDecodeError:
            raise DaCliError("server returned invalid JSON: " + response.text)
        if not isinstance(info, dict) or info.get("task_id") is None:
            raise DaCliError("server response missing task_id: " + str(info))
        wait_for_server(True, info["task_id"], apikey, apiurl)
    elif response.status_code == 404:
        # The documented endpoint returns a success code even for a missing
        # file, but some servers (or proxies in front of them) answer 404.
        # The file is already gone, so treat it as a successful delete rather
        # than retrying forever (the local file no longer exists, so a retry
        # could never clear the failure).
        pass
    elif response.status_code != 204:
        raise DaCliError(f"playground delete ({folder}) returned {response.status_code}: {response.text}")
    UPLOADED_NAMES.setdefault((folder, project or "default"), set()).discard(filename)


def playground_delete_files(apiurl: str, apikey: str, playground: str, paths: list[str]) -> list[str]:
    """Delete locally-deleted files from the Playground; returns failed paths."""
    project = playground if playground and playground != "default" else None
    failed = []
    for path in paths:
        folder = playground_folder_of(path)
        if folder is None:
            continue
        try:
            playground_delete_server_file(apiurl, apikey, folder, server_file_name(path), project)
        except DaCliError as err:
            failed.append(path)
            click.secho(f"\n{err}", fg="red")
    return failed


def playground_reconcile(apiurl: str, apikey: str, playground: str) -> None:
    """Delete Playground files this CLI uploaded that no longer exist locally.

    This automates cleaning up the Playground: files the CLI itself uploaded
    (tracked in UPLOADED_NAMES) that are still on the server but gone from the
    package directory are deleted. Files uploaded through the web UI or other
    means are never touched. Errors are printed and skipped.
    """
    project = playground if playground and playground != "default" else None
    local_names: dict[str, set[str]] = {
        folder: set() for folder in ("questions", "sources", "static", "templates", "modules")
    }
    for path in WATCHED_FILES:
        folder = playground_folder_of(path)
        if folder in local_names:
            local_names[folder].add(server_file_name(path))
    for folder in ("questions", "sources", "static", "templates", "modules"):
        key = (folder, project or "default")
        owned = UPLOADED_NAMES.get(key, set())
        if not owned:
            continue
        params = {"folder": folder}
        if project:
            params["project"] = project
        try:
            response = http_get(apiurl + "/api/playground", params=params, headers={"X-API-Key": apikey}, timeout=600)
        except requests.exceptions.RequestException as err:
            click.secho(f"\n{err.__class__.__name__}: {err}", fg="red")
            continue
        if response.status_code != 200:
            click.secho(f"\nplayground list ({folder}) returned {response.status_code}: {response.text}", fg="red")
            continue
        try:
            server_files = response.json()
        except requests.exceptions.JSONDecodeError:
            click.secho(f"\nplayground list ({folder}) returned invalid JSON: {response.text}", fg="red")
            continue
        if not isinstance(server_files, list):
            continue
        for name in sorted(server_files):
            if name in owned and name not in local_names[folder]:
                try:
                    playground_delete_server_file(apiurl, apikey, folder, name, project)
                    click.secho(f"""Deleted from Playground: {name}""", fg="yellow")
                except DaCliError as err:
                    click.secho(f"\n{err}", fg="red")


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
                raise TypeError
    except (yaml.YAMLError, TypeError, OSError):
        raise click.BadParameter("File is not a usable docassemblecli config.")
    return (config, env)


def parse_project_command_config(data) -> tuple[list, dict[str, dict]]:
    if isinstance(data, list):
        return data, {"install": {}, "watch": {}}
    if not isinstance(data, dict):
        raise TypeError

    servers = data.get("servers", [])
    if servers is None:
        servers = []
    if not isinstance(servers, list):
        raise TypeError

    sections = {}
    for command_name in ("install", "watch"):
        section = data.get(command_name, {})
        if section is None:
            section = {}
        if not isinstance(section, dict):
            raise TypeError
        sections[command_name] = section
    return servers, sections


def load_project_command_config(directory: str, command_name: str) -> tuple[str, list, dict]:
    config_path = os.path.abspath(os.path.join(directory, PROJECT_CONFIG))
    if not os.path.isfile(config_path):
        raise click.BadParameter(f'"{config_path}" does not exist.', param_hint="--project-config")
    try:
        with open(config_path, "r", encoding="utf-8") as fp:
            data = yaml.load(fp, Loader=yaml.FullLoader)
        servers, sections = parse_project_command_config(data)
    except click.BadParameter:
        raise
    except (yaml.YAMLError, TypeError, OSError):
        raise click.BadParameter("File is not a usable project config.", param_hint="--project-config")
    return config_path, servers, sections.get(command_name, {})


def merge_command_config(selected_server: dict, command_config: dict, api_provided: bool = False) -> dict:
    merged_server = dict(selected_server)
    for key, value in command_config.items():
        if key == "server":
            continue
        if api_provided and key in ("apiurl", "apikey", "name"):
            continue
        merged_server[key] = value
    if not merged_server.get("name") and merged_server.get("apiurl"):
        merged_server["name"] = name_from_url(merged_server["apiurl"])
    return merged_server


def combine_config_envs(
    primary_config: tuple[str | None, list], secondary_config: tuple[str | None, list]
) -> tuple[str | None, list]:
    primary_cfg, primary_env = primary_config
    secondary_cfg, secondary_env = secondary_config
    effective_cfg = primary_cfg or secondary_cfg
    combined_env = list(primary_env or []) + list(secondary_env or [])
    return effective_cfg, combined_env


def resolve_command_server(
    command_name: str,
    directory: str,
    config: tuple[str | None, list],
    api: tuple[str | None, str | None],
    server: str,
    project_config: bool,
) -> dict:
    command_config = {}
    if project_config:
        project_config_path = os.path.abspath(os.path.join(directory, PROJECT_CONFIG))
        if os.path.isfile(project_config_path):
            project_cfg, project_env, command_config = load_project_command_config(directory, command_name)
            config = combine_config_envs((project_cfg, project_env), config)
    configured_server = command_config.get("server", "")
    selected_server = select_server(*config, *api, server or configured_server, directory=directory)
    if project_config:
        selected_server = merge_command_config(selected_server, command_config, api_provided=bool(api[0] and api[1]))
    return selected_server


def remove_server_references_from_project_config(directory: str, server_name: str) -> tuple[bool, str]:
    config_path, env, sections = load_or_create_project_config(directory)
    updated_env = [item for item in env if item.get("name") != server_name]
    updated_sections = {
        "install": dict(sections.get("install", {})),
        "watch": dict(sections.get("watch", {})),
    }

    changed = len(updated_env) != len(env)
    for command_name in ("install", "watch"):
        if updated_sections[command_name].get("server") == server_name:
            updated_sections[command_name].pop("server", None)
            changed = True

    if not changed:
        return False, config_path
    return save_project_config(config_path, updated_env, updated_sections), config_path


def resolve_command_server_with_cleanup(
    command_name: str,
    directory: str,
    config: tuple[str | None, list],
    api: tuple[str | None, str | None],
    server: str,
    project_config: bool,
) -> dict:
    try:
        return resolve_command_server(command_name, directory, config, api, server, project_config)
    except click.BadParameter as err:
        if not project_config or server:
            raise
        project_config_path = os.path.abspath(os.path.join(directory, PROJECT_CONFIG))
        if not os.path.isfile(project_config_path):
            raise
        project_cfg, _, command_config = load_project_command_config(directory, command_name)
        configured_server = command_config.get("server", "")
        if getattr(err, "message", "") != f'Server "{configured_server}" was not found.':
            raise
        if not click.confirm(
            f'Server "{configured_server}" from the local project config was not found. Remove it from {project_cfg}?',
            default=False,
        ):
            raise
        removed, config_path = remove_server_references_from_project_config(directory, configured_server)
        if removed:
            click.echo(f'Server "{configured_server}" has been removed from {config_path}.')
            return resolve_command_server(command_name, directory, config, api, server, project_config)
        raise


# -----------------------------------------------------------------------------
# utility functions
# -----------------------------------------------------------------------------


def name_from_url(url: str) -> str:
    if not url:
        return ""
    return urlparse(url).netloc


def display_servers(env: list | None = None) -> list[str]:
    if not env:
        return ["No servers found."]
    servers = []
    for idx, item in enumerate(env):
        server_label = item.get("name") or item.get("apiurl", "")
        if idx:
            servers.append(server_label)
        else:
            servers.append(server_label + " (default)")
        if "apiurl" in item:
            servers.append(f"""  apiurl: {item["apiurl"]}""")
        if "playground" in item:
            servers.append(f"""  playground: {item["playground"]}""")
        if "directory" in item:
            servers.append(f"""  directory: {item["directory"]}""")
        if "startup" in item:
            servers.append(f"""  startup: {item["startup"]}""")
    return servers


def display_project_command_sections(sections: dict[str, dict] | None) -> list[str]:
    if not sections:
        return []
    lines = []
    for command_name in ("install", "watch"):
        section = sections.get(command_name, {}) or {}
        if not section:
            continue
        lines.append(f"{command_name}:")
        for key in ("server", "playground", "startup", "sweep_interval"):
            if key in section:
                lines.append(f"  {key}: {section[key]}")
    return lines


def select_server(
    cfg: str | None = None,
    env: list | None = None,
    apiurl: str | None = None,
    apikey: str | None = None,
    server: str | None = "",
    **kwargs,
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
    env: list | None = None, apiurl: str = "", apikey: str = "", directory: str = "", playground: str = ""
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
    except OSError as err:
        click.echo(f"Unable to save {cfg} file. {err.__class__.__name__}: {err}")
        return False
    return True


def load_or_create_project_config(directory: str) -> tuple[str, list, dict[str, dict]]:
    config_path = os.path.abspath(os.path.join(directory, PROJECT_CONFIG))
    if not os.path.isfile(config_path):
        return config_path, [], {"install": {}, "watch": {}}
    try:
        with open(config_path, "r", encoding="utf-8") as fp:
            data = yaml.load(fp, Loader=yaml.FullLoader)
        env, sections = parse_project_command_config(data)
    except (yaml.YAMLError, TypeError, OSError):
        raise click.BadParameter("File is not a usable project config.", param_hint="--project-config")
    return config_path, env, sections


def save_project_config(config_path: str, env: list, sections: dict[str, dict]) -> bool:
    try:
        with open(config_path, "w", encoding="utf-8") as fp:
            yaml.dump({"servers": env, "install": sections.get("install", {}), "watch": sections.get("watch", {})}, fp)
    except OSError as err:
        click.echo(f"Unable to save {config_path} file. {err.__class__.__name__}: {err}")
        return False
    return True


def prompt_for_config_scope() -> str:
    valid_choices = {
        "g": "global",
        "global": "global",
        "l": "local",
        "local": "local",
    }
    while True:
        choice = (
            click.prompt(
                "Use the global config file or the local project config? ([g]lobal, [l]ocal)",
                default="global",
                show_default=True,
            )
            .strip()
            .lower()
        )
        if choice in valid_choices:
            return valid_choices[choice]
        click.echo('Please enter "g", "global", "l", or "local".')


def prompt_for_optional_playground() -> str | None:
    playground = click.prompt(
        "Default Playground project for this server (leave blank to skip; use 'default' for the default Playground)",
        default="",
        show_default=False,
    ).strip()
    return playground or None


def prompt_for_optional_directory() -> str | None:
    while True:
        directory = click.prompt(
            "Package directory to associate with this server (leave blank to skip)",
            default="",
            show_default=False,
        ).strip()
        if not directory:
            return None
        try:
            return validate_package_directory(None, None, directory)
        except click.BadParameter as err:
            click.echo(err.message)


def prompt_for_command_default(command_name: str) -> bool:
    return click.confirm(
        f"Use this server as the default for {command_name} in the local project config?",
        default=False,
        show_default=True,
    )


def prompt_for_command_playground(command_name: str) -> str | None:
    playground = click.prompt(
        f"Default Playground project for {command_name} in this project (leave blank to skip; use 'default' for the default Playground)",
        default="",
        show_default=False,
    ).strip()
    return playground or None


def prompt_for_watch_startup() -> str | None:
    if click.confirm("Install once when watch starts for this project?", default=False, show_default=True):
        return "install"
    return None


def resolve_project_config_directory(directory: str | None) -> str:
    if directory is not None:
        return validate_package_directory(None, None, directory)
    suggested_directory = os.getcwd()
    try:
        return validate_package_directory(None, None, suggested_directory)
    except click.BadParameter:
        pass
    while True:
        chosen_directory = click.prompt(
            "Package directory that should contain the local project config",
            default=suggested_directory,
            show_default=True,
        ).strip()
        try:
            return validate_package_directory(None, None, chosen_directory)
        except click.BadParameter as err:
            click.echo(err.message)


def resolve_config_target(
    config_path: str | None,
    use_project_config: bool,
    use_global_config: bool,
    directory: str | None,
) -> tuple[str, str, list, dict[str, dict] | None]:
    if use_project_config and (use_global_config or config_path):
        raise click.BadParameter("Cannot be combined with global config options.", param_hint="--project-config")

    if use_project_config:
        target_scope = "local"
    elif use_global_config or config_path:
        target_scope = "global"
    else:
        target_scope = prompt_for_config_scope()

    if target_scope == "local":
        project_directory = resolve_project_config_directory(directory)
        cfg, env, sections = load_or_create_project_config(project_directory)
        return target_scope, cfg, env, sections

    cfg, env = validate_and_load_or_create_config(None, None, config_path or DEFAULT_CONFIG)
    return target_scope, cfg, env, None


def prompt_for_api(
    retry: str | None = False,
    previous_url: str | None = None,
    previous_key: str | None = None,
) -> tuple[str, str]:
    if retry and not click.confirm("Do you want to try another URL and API key?", default=True):
        raise click.Abort()
    apiurl = click.prompt(
        """Base URL of your docassemble server (e.g., https://da.example.com)""",
        type=APIURLType(),
        default=previous_url,
    )
    apikey = click.prompt(f"""API key of admin or developer user on {apiurl}""", default=previous_key).strip()
    return apiurl, apikey


def ensure_api_credentials(
    apiurl: str | None = None,
    apikey: str | None = None,
) -> tuple[str, str]:
    if not apiurl or not apikey:
        apiurl, apikey = prompt_for_api()
    while not test_apiurl_apikey(apiurl=apiurl, apikey=apikey):
        apiurl, apikey = prompt_for_api(retry=True, previous_url=apiurl, previous_key=apikey)
    return apiurl, apikey


def test_apiurl_apikey(apiurl: str, apikey: str) -> bool:
    click.echo("Testing the URL and API key...")
    try:
        api_test = http_get(apiurl + "/api/package", headers={"X-API-Key": apikey})
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
    except requests.exceptions.RequestException as err:
        click.secho(f"""\n{err.__class__.__name__}""", fg="red")
        click.echo(f"""{err}\n""")
        return False
    click.secho(f"Success!{BELL}", fg="green")
    return True


def add_server_to_env(
    cfg: str | None = None,
    env: list | None = None,
    apiurl: str | None = None,
    apikey: str | None = None,
    directory: str | None = None,
    playground: str | None = None,
    validate_api: bool = True,
):
    if validate_api:
        apiurl, apikey = ensure_api_credentials(apiurl=apiurl, apikey=apikey)
    env = add_or_update_env(env=env, apiurl=apiurl, apikey=apikey, directory=directory, playground=playground)
    if cfg and save_config(cfg, env):
        click.echo(f"""Configuration saved: {cfg}""")
    return env


def apply_project_command_defaults(
    sections: dict[str, dict],
    server_name: str,
    configure_install: bool,
    install_playground: str | None,
    configure_watch: bool,
    watch_playground: str | None,
    watch_startup: str | None,
) -> dict[str, dict]:
    updated_sections = {
        "install": dict(sections.get("install", {})),
        "watch": dict(sections.get("watch", {})),
    }
    if configure_install:
        updated_sections["install"]["server"] = server_name
        if install_playground:
            updated_sections["install"]["playground"] = install_playground
        else:
            updated_sections["install"].pop("playground", None)
    if configure_watch:
        updated_sections["watch"]["server"] = server_name
        if watch_playground:
            updated_sections["watch"]["playground"] = watch_playground
        else:
            updated_sections["watch"].pop("playground", None)
        if watch_startup == "install":
            updated_sections["watch"]["startup"] = watch_startup
        else:
            updated_sections["watch"].pop("startup", None)
    return updated_sections


def wait_for_server(playground: bool, task_id: str, apikey: str, apiurl: str, server_version_da: str = "0"):
    """Poll the server until the install/restart task completes.

    Returns None on success and raises ServerStatusError on failure, so
    callers never have to distinguish False from error strings.
    """

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
                r = http_get(full_url, params={"task_id": task_id}, headers={"X-API-Key": apikey}, timeout=600)
            except requests.exceptions.RequestException:
                time.sleep(1)
                tries += 1
                continue
            if r.status_code != 200:
                raise ServerStatusError("server status returned " + str(r.status_code) + ": " + r.text)
            try:
                info = r.json()
            except requests.exceptions.JSONDecodeError:
                raise ServerStatusError("server returned invalid JSON for " + full_url + ": " + r.text)
            if not isinstance(info, dict):
                raise ServerStatusError("server returned non-object JSON for " + full_url + ": " + str(info))
            try:
                if info["status"] == "completed" or info["status"] == "unknown":
                    break
            except KeyError:
                raise ServerStatusError("server response missing status field for " + full_url + ": " + str(info))
            time.sleep(1)
            tries += 1
        after_wait_for_server = time.time()
        success = False
        if playground:
            if info.get("status", None) == "completed":
                success = True
        elif info.get("ok", False):
            success = True
        try:
            server_is_new_enough = packaging_version.parse(server_version_da) >= packaging_version.parse("1.5.3")
        except packaging_version.InvalidVersion:
            server_is_new_enough = False
        if not (server_version_da == "norestart" or server_is_new_enough):
            if DEBUG:
                click.echo(f"""\rPackage install duration: {(after_wait_for_server - before_wait_for_server):.2f}s""")
                click.echo("""\rManually waiting for background processes.""")
            time.sleep(after_wait_for_server - before_wait_for_server)
        if success:
            return
        if not playground and "error_message" in info and isinstance(info["error_message"], str):
            raise ServerStatusError(info["error_message"])
        raise ServerStatusError(
            f"server did not report a successful install (status: {info.get('status', 'unknown')!r})"
        )

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
        except Exception as e:  # noqa: BLE001
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


def task_id_from_response(response, endpoint: str) -> str:
    """Extract a task_id from a server response; raise DaCliError on malformed bodies."""
    try:
        info = response.json()
    except requests.exceptions.JSONDecodeError:
        raise DaCliError(endpoint + " returned invalid JSON: " + response.text)
    if not isinstance(info, dict):
        raise DaCliError(endpoint + " returned non-object JSON: " + str(info))
    task_id = info.get("task_id")
    if task_id is None:
        raise DaCliError(endpoint + " response missing task_id: " + str(info))
    return task_id


def _stable_checksum(full_path: str) -> str | None:
    """Return the checksum of a file read to stable content, or None.

    The file is stat'ed before and after a hashing read; if it changed in
    between it is re-read (up to three attempts), so the checksum describes
    content that was not being edited.
    """
    for _attempt in range(3):
        try:
            st = os.stat(full_path)
        except OSError as err:
            click.secho(f"{err}", fg="red")
            return None
        hasher = xxhash.xxh64()
        try:
            with open(full_path, "rb") as fp:
                while chunk := fp.read(CHUNK_SIZE):
                    hasher.update(chunk)
        except OSError as err:
            click.secho(f"{err} while reading {full_path}.", fg="red")
            return None
        try:
            st_after = os.stat(full_path)
        except OSError:
            continue
        if (st.st_mtime, st.st_size) != (st_after.st_mtime, st_after.st_size):
            continue
        return hasher.hexdigest()
    click.secho(f"{full_path} kept changing while it was being archived; skipping it.", fg="red")
    return None


def _archive_one_file(zf, full_path: str, directory: str) -> str | None:
    """Write one file into the zip, hashing its content.

    The file is first read to stable content; only then is it written to the
    zip, and the write is verified to produce the same checksum, so the
    archive never contains a torn snapshot of a file that was being edited.
    Returns the checksum of exactly what was archived, or None if the file
    could not be read.
    """
    arcname = os.path.relpath(full_path, os.path.join(directory, ".."))
    verified_hash = _stable_checksum(full_path)
    if verified_hash is None:
        return None
    write_hasher = xxhash.xxh64()
    try:
        with open(full_path, "rb") as fp, zf.open(arcname, "w", force_zip64=True) as dest:
            while chunk := fp.read(CHUNK_SIZE):
                write_hasher.update(chunk)
                dest.write(chunk)
    except OSError as err:
        click.secho(f"{err} while archiving {full_path}.", fg="red")
        return None
    if write_hasher.hexdigest() == verified_hash:
        return verified_hash
    click.secho(f"{full_path} changed while it was being archived; skipping it.", fg="red")
    return None


def _is_excluded(
    the_file: str, root: str, adjusted_root: str, root_directory: str | None, to_ignore: list[str]
) -> bool:
    """Return whether `the_file` is left out of the package archive: an
    archive-excluded extension, the root .gitignore file itself, or a path
    git ignores (via `git ls-files -i -o`)."""
    return bool(
        is_archive_excluded(the_file)
        or (the_file == ".gitignore" and root == root_directory)
        or os.path.normpath(os.path.join(adjusted_root, the_file)) in to_ignore
    )


def package_installer(
    directory, apiurl, apikey, playground, restart, dry_run=False, show_files=False, announced_conflicts=None
):
    """Install the package directory on the server.

    Returns an archive map {relative_path: checksum} describing exactly what
    was uploaded ({} for a dry run), and raises DaCliError on any failure. The
    map lets the watch loop mark files as uploaded only when the server
    confirmed receiving their exact content. In Playground mode, files whose
    names collide in the flat Playground layout are left out of the archive
    with a warning; pass `announced_conflicts` (the set of conflicting paths
    the caller already warned about) to suppress repeated warnings.
    """
    with tempfile.NamedTemporaryFile(suffix=".zip") as archive:
        archive_map = {}
        archived_files = []
        skipped_files = []
        root_directory = None
        has_python_files = False
        this_package_name = None
        dependencies = {}
        try:
            ignore_process = subprocess.run(
                ["git", "ls-files", "-i", "--directory", "-o", "--exclude-standard"],
                capture_output=True,
                text=True,
                cwd=directory,
                check=False,
            )
            ignore_process.check_returncode()
            raw_ignore = ignore_process.stdout.splitlines()
        except (subprocess.CalledProcessError, OSError):
            raw_ignore = []
        to_ignore = [path.rstrip("/") for path in raw_ignore]
        with zipfile.ZipFile(archive, compression=zipfile.ZIP_DEFLATED, mode="w") as zf:
            walked_dirs = []
            for root, dirs, files in os.walk(directory, topdown=True):
                adjusted_root = os.path.relpath(root, directory)
                dirs[:] = [
                    d
                    for d in dirs
                    if d not in EXCLUDED_DIRECTORIES
                    and not d.endswith(".egg-info")
                    and os.path.normpath(os.path.join(adjusted_root, d)) not in to_ignore
                ]
                walked_dirs.append((root, adjusted_root, files))
            for root, adjusted_root, files in walked_dirs:
                if root_directory is None and package_metadata_files_present(root):
                    root_directory = root
                    this_package_name, dependencies = load_package_metadata(root, files)
            conflict_skip_paths: set[str] = set()
            if playground:
                candidates = _readable_paths(
                    [
                        os.path.join(root, the_file)
                        for root, adjusted_root, files in walked_dirs
                        for the_file in files
                        if not _is_excluded(the_file, root, adjusted_root, root_directory, to_ignore)
                    ]
                )
                if playground_name_conflicts(candidates):
                    # Read each conflicted candidate once to see which of them
                    # would actually be archived; only those count as
                    # conflicting. A candidate that can not be read (or keeps
                    # changing) is skipped and reported, and its readable
                    # same-named sibling is released to sync normally.
                    all_conflicted = playground_conflict_paths(candidates)
                    would_archive: set[str] = set()
                    for full_path in sorted(all_conflicted):
                        if _stable_checksum(full_path) is not None:
                            would_archive.add(full_path)
                        else:
                            skipped_files.append(full_path)
                    still_conflicted = playground_conflict_paths(list(would_archive))
                    unannounced = [
                        pair
                        for pair in playground_name_conflicts(list(still_conflicted))
                        if not announced_conflicts or not set(pair[:2]).issubset(announced_conflicts)
                    ]
                    if unannounced:
                        click.secho(format_playground_conflict_skip(unannounced), fg="yellow")
                    # The main loop skips the files that are still conflicted
                    # plus the ones that failed the readiness pass (already
                    # reported); released files archive normally.
                    conflict_skip_paths = still_conflicted | (all_conflicted - would_archive)
            for root, adjusted_root, files in walked_dirs:
                for the_file in files:
                    if (
                        _is_excluded(the_file, root, adjusted_root, root_directory, to_ignore)
                        or os.path.join(root, the_file) in conflict_skip_paths
                    ):
                        continue
                    if (
                        not has_python_files
                        and the_file.endswith(".py")
                        and adjusted_root.startswith("docassemble" + os.sep)
                        and not (the_file in ("setup.py", "setup.cfg", "pyproject.toml") and root == root_directory)
                        and the_file != "__init__.py"
                    ):
                        has_python_files = True
                    full_path = os.path.join(root, the_file)
                    archived = _archive_one_file(zf, full_path, directory)
                    if archived is None:
                        skipped_files.append(full_path)
                        continue
                    archive_map[os.path.relpath(full_path, os.path.join(directory, ".."))] = archived
                    archived_files.append(os.path.relpath(full_path, directory))
        archive.seek(0)
        if skipped_files:
            click.secho("Files that could not be read were skipped:", fg="yellow")
            for skipped in skipped_files:
                click.echo("  " + skipped)
        if not archived_files:
            if conflict_skip_paths:
                raise DaCliError(
                    "no files could be archived from the package directory "
                    "(every file was skipped because of Playground name conflicts or could not be read)"
                )
            raise DaCliError("no files could be archived from the package directory")
        archived_files.sort()
        if restart == "no":
            should_restart = False
        elif restart == "yes" or has_python_files:
            should_restart = True
        elif len(dependencies) > 0 or this_package_name:
            try:
                r = http_get(apiurl + "/api/package", headers={"X-API-Key": apikey}, timeout=600)
            except requests.exceptions.RequestException as err:
                raise DaCliError(f"{err.__class__.__name__}: {err}") from err
            if r.status_code != 200:
                raise DaCliError("/api/package returned " + str(r.status_code) + ": " + r.text)
            try:
                installed_packages = r.json()
            except requests.exceptions.JSONDecodeError:
                raise DaCliError("/api/package returned invalid JSON: " + r.text)
            already_installed = False
            for package_info in installed_packages:
                package_info["alt_name"] = re.sub(r"^docassemble\.", "docassemble-", package_info["name"])
                for dependency_name, dependency_info in dependencies.items():
                    if dependency_name in (package_info["name"], package_info["alt_name"]):
                        condition = True
                        if dependency_info["operator"]:
                            try:
                                if dependency_info["operator"] == "==":
                                    condition = packaging_version.parse(
                                        package_info["version"]
                                    ) == packaging_version.parse(dependency_info["version"])
                                elif dependency_info["operator"] == "<=":
                                    condition = packaging_version.parse(
                                        package_info["version"]
                                    ) <= packaging_version.parse(dependency_info["version"])
                                elif dependency_info["operator"] == ">=":
                                    condition = packaging_version.parse(
                                        package_info["version"]
                                    ) >= packaging_version.parse(dependency_info["version"])
                                elif dependency_info["operator"] == "<":
                                    condition = packaging_version.parse(
                                        package_info["version"]
                                    ) < packaging_version.parse(dependency_info["version"])
                                elif dependency_info["operator"] == ">":  # pragma: no branch
                                    condition = packaging_version.parse(
                                        package_info["version"]
                                    ) > packaging_version.parse(dependency_info["version"])
                            except packaging_version.InvalidVersion:
                                condition = False
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
        if should_restart and not dry_run:
            try:
                server_packages = http_get(apiurl + "/api/package", headers={"X-API-Key": apikey}, timeout=600)
                if server_packages.status_code != 200:
                    if server_packages.status_code == 403:
                        click.secho("""\nThe API KEY is invalid.""", fg="red")
                    raise DaCliError(
                        "/api/package returned " + str(server_packages.status_code) + ": " + server_packages.text
                    )
                try:
                    installed_packages = server_packages.json()
                except requests.exceptions.JSONDecodeError:
                    raise DaCliError("/api/package returned invalid JSON: " + server_packages.text)
                for package in installed_packages:
                    if package.get("name", "") == "docassemble.base":
                        server_version_da = package.get("version", "0")
            except requests.exceptions.RequestException as err:
                raise DaCliError(f"{err.__class__.__name__}: {err}") from err
            click.secho("Server will restart.", fg="yellow")
        if not should_restart:
            server_version_da = "norestart"
            data["restart"] = "0"
        if DEBUG and not dry_run:
            click.echo(f"""Server version: {server_version_da}.""")
        if dry_run:
            show_dry_run_package_install(
                playground=playground,
                should_restart=should_restart,
                archived_files=archived_files,
                show_files=show_files,
            )
            return {}
        if playground:
            if playground != "default":
                data["project"] = playground
            project_endpoint = apiurl + "/api/playground/project"
            click.secho("Checking Playground project...", fg="cyan")
            try:
                project_list = http_get(project_endpoint, headers={"X-API-Key": apikey}, timeout=600)
            except requests.exceptions.RequestException as err:
                raise DaCliError(f"{err.__class__.__name__}: {err}") from err
            if project_list.status_code == 200:
                try:
                    existing_projects = project_list.json()
                except requests.exceptions.JSONDecodeError:
                    raise DaCliError("playground list of projects GET returned invalid JSON: " + project_list.text)
                if not playground_project_exists(existing_projects, playground):
                    try:
                        click.secho(f'''Creating Playground project "{playground}"...''', fg="cyan")
                        created = http_post(
                            project_endpoint,
                            data={"project": playground},
                            headers={"X-API-Key": apikey},
                            timeout=600,
                        )
                    except requests.exceptions.RequestException as err:
                        raise DaCliError(f"{err.__class__.__name__}: {err}") from err
                    if created.status_code != 204:
                        raise DaCliError(
                            "create project POST returned " + str(created.status_code) + ": " + created.text
                        )
            else:
                click.echo("\n")
                raise DaCliError(
                    "playground list of projects GET returned "
                    + str(project_list.status_code)
                    + ": "
                    + project_list.text
                )
            try:
                click.secho("Uploading package to Playground...", fg="cyan")
                r = http_post(
                    apiurl + "/api/playground_install",
                    data=data,
                    files={"file": archive},
                    headers={"X-API-Key": apikey},
                    timeout=600,
                )
            except requests.exceptions.RequestException as err:
                raise DaCliError(f"{err.__class__.__name__}: {err}") from err
            if r.status_code == 400:
                try:
                    error_message = r.json()
                except requests.exceptions.JSONDecodeError:
                    error_message = ""
                if "project" not in data or error_message != "Invalid project.":
                    raise DaCliError("playground_install POST returned " + str(r.status_code) + ": " + r.text)
                try:
                    r = http_post(
                        apiurl + "/api/playground/project",
                        data={"project": data["project"]},
                        headers={"X-API-Key": apikey},
                        timeout=600,
                    )
                except requests.exceptions.RequestException as err:
                    raise DaCliError(f"{err.__class__.__name__}: {err}") from err
                if r.status_code != 204:
                    raise DaCliError(
                        "needed to create playground project but POST to api/playground/project returned "
                        + str(r.status_code)
                        + ": "
                        + r.text
                    )
                archive.seek(0)
                try:
                    r = http_post(
                        apiurl + "/api/playground_install",
                        data=data,
                        files={"file": archive},
                        headers={"X-API-Key": apikey},
                        timeout=600,
                    )
                except requests.exceptions.RequestException as err:
                    raise DaCliError(f"{err.__class__.__name__}: {err}") from err
            if r.status_code == 200:
                task_id = task_id_from_response(r, "playground_install POST")
                wait_for_server(
                    playground=True,
                    task_id=task_id,
                    apikey=apikey,
                    apiurl=apiurl,
                    server_version_da=server_version_da,
                )
                announce_installed()
            elif r.status_code == 204:
                announce_installed()
            else:
                click.echo("\n")
                raise DaCliError("playground_install POST returned " + str(r.status_code) + ": " + r.text)
        else:
            try:
                r = http_post(
                    apiurl + "/api/package",
                    data=data,
                    files={"zip": archive},
                    headers={"X-API-Key": apikey},
                    timeout=600,
                )
            except requests.exceptions.RequestException as err:
                raise DaCliError(f"{err.__class__.__name__}: {err}") from err
            if r.status_code != 200:
                raise DaCliError("package POST returned " + str(r.status_code) + ": " + r.text)
            task_id = task_id_from_response(r, "package POST")
            wait_for_server(
                playground=False,
                task_id=task_id,
                apikey=apikey,
                apiurl=apiurl,
                server_version_da=server_version_da,
            )
            announce_installed()
            if not should_restart:
                try:
                    r = http_post(apiurl + "/api/clear_cache", headers={"X-API-Key": apikey}, timeout=600)
                except requests.exceptions.RequestException as err:
                    raise DaCliError(f"{err.__class__.__name__}: {err}") from err
                if r.status_code != 204:
                    raise DaCliError("clear_cache returned " + str(r.status_code) + ": " + r.text)
        return archive_map


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
@click.option("--dry-run", is_flag=True, help="Show what would be installed without uploading anything.")
@click.option("--show-files", is_flag=True, help="With --dry-run, list the files that would be uploaded.")
def install(directory, config, project_config, api, server, playground, no_playground, restart, dry_run, show_files):
    """
    Install a docassemble package on a docassemble server.

    `install` tries to get API info from the --api option first (if used), then from the first server listed in the ~/.docassemblecli file if it exists (unless the --config option is used), then it tries to use environmental variables, and finally it prompts the user directly.
    """
    selected_server = resolve_command_server_with_cleanup("install", directory, config, api, server, project_config)
    if no_playground or playground == "":
        playground = ""
    elif playground is None and project_config and "playground" in selected_server:
        playground = selected_server["playground"]
    click.echo(f"""Server: {selected_server["name"]}""")
    if not playground:
        click.echo("Location: Package")
    else:
        click.echo(f"""Location: Playground "{playground}" """)
    if dry_run:
        click.secho(
            f"""[{datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")}] Dry run: previewing install...""",
            fg="cyan",
        )
    else:
        click.secho(
            f"""[{datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")}] Installing...""", fg="yellow"
        )
    try:
        package_installer(
            directory=directory,
            apiurl=selected_server["apiurl"],
            apikey=selected_server["apikey"],
            playground=playground,
            restart=restart,
            dry_run=dry_run,
            show_files=show_files,
        )
    except DaCliError as err:
        click.secho(f"""\n{err}""", fg="red")
        return 1
    return 0


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
@click.option(
    "--no-playground",
    is_flag=True,
    help="Download the installed package, ignoring any Playground option.",
)
@click.option("--overwrite/--no-overwrite", default=False, show_default=True, help="Overwrite existing files.")
@click.argument("package")
def download(config, api, server, playground, no_playground, overwrite, package):
    """
    Download a docassemble package from a docassemble server or Playground.
    """
    if no_playground or playground == "":
        playground = ""
    selected_server = select_server(*config, *api, server)
    package_name = normalize_package_name(package)
    package_file_name = re.sub(r"docassemble\.", "docassemble-", package_name)
    with tempfile.TemporaryFile(suffix=".zip") as archive:
        try:
            if playground:
                params = {"folder": "packages", "filename": package_name}
                if playground != "default":
                    params["project"] = playground
                response = http_get(
                    selected_server["apiurl"] + "/api/playground",
                    params=params,
                    stream=True,
                    timeout=600,
                    headers={"X-API-Key": selected_server["apikey"]},
                )
                if response.status_code == 404:
                    click.secho("\nPackage not found.", fg="red")
                    return 1
                response.raise_for_status()
            else:
                response = http_get(
                    selected_server["apiurl"] + "/api/package",
                    headers={"X-API-Key": selected_server["apikey"]},
                    timeout=600,
                )
                if response.status_code != 200:
                    click.secho("\nUnable to connect to server.", fg="red")
                    return 1
                zip_file_number = None
                for item in response.json():
                    if item["name"] == package_name:
                        zip_file_number = item.get("zip_file_number")
                        break
                if zip_file_number is None:
                    click.secho("\nPackage installed but is not downloadable.", fg="red")
                    return 1
                response = http_get(
                    selected_server["apiurl"] + "/api/file/" + str(zip_file_number),
                    stream=True,
                    timeout=600,
                    headers={"X-API-Key": selected_server["apikey"]},
                )
                response.raise_for_status()
        except requests.exceptions.HTTPError as err:
            click.secho("\nError downloading package: " + str(err), fg="red")
            return 1
        except requests.exceptions.RequestException as err:
            click.secho(f"""\n{err.__class__.__name__}""", fg="red")
            raise click.ClickException(f"""{err}\n""")

        for chunk in response.iter_content(8192):
            archive.write(chunk)
        archive.seek(0)

        with zipfile.ZipFile(archive, mode="r") as zf:
            if not overwrite:
                for file_info in zf.infolist():
                    if os.path.exists(file_info.filename):
                        click.secho(
                            "\nUnpacking the package here would overwrite existing files "
                            + f"({file_info.filename}). Use --overwrite if you want to overwrite existing files.",
                            fg="red",
                        )
                        return 1
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
        response = http_delete(
            selected_server["apiurl"] + "/api/package",
            params=data,
            headers={"X-API-Key": selected_server["apikey"]},
            timeout=600,
        )
        if response.status_code != 200:
            raise DaCliError("package DELETE returned " + str(response.status_code) + ": " + response.text)
        task_id = task_id_from_response(response, "package DELETE")
        wait_for_server(False, task_id, selected_server["apikey"], selected_server["apiurl"])
    except DaCliError as err:
        click.secho(f"""\n{err}""", fg="red")
        return 1
    except requests.exceptions.RequestException as err:
        click.secho(f"""\n{err.__class__.__name__}: {err}""", fg="red")
        return 1
    click.secho(
        f"""[{datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")}] Uninstalled.{BELL}""",
        fg="green",
    )
    return 0


# -----------------------------------------------------------------------------
# watchdog & xxhash
# -----------------------------------------------------------------------------


def calculate_checksum(filepath: str) -> str:
    hasher = xxhash.xxh64()
    try:
        with open(filepath, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                hasher.update(chunk)
    except FileNotFoundError:
        return ""
    except OSError as e:
        click.secho(f"""{e} while calculating checksum.""", fg="red")
        return ""
    return hasher.hexdigest()


def is_archive_excluded(filename: str) -> bool:
    """Return True for files the archive builder can never upload.

    These match the exclusions in package_installer (and the docassemble
    server's own install filter), so the watcher must not track them either:
    they could never reach the server and would otherwise be marked uploaded.
    """
    return (
        filename.endswith(("~", ".pyc", ".swp", ".tmp", ".swx"))
        or filename.startswith(("#", ".#"))
        or ".tmp." in filename
    )


def scan_directory(directory):
    if DEBUG:
        click.secho("Scanning files...", fg="cyan")
    for current_directory, subdirectories, files in os.walk(directory):
        excluded_directories = EXCLUDED_DIRECTORIES
        subdirectories[:] = [d for d in subdirectories if d not in excluded_directories]
        for file in files:
            filepath = os.path.join(current_directory, file)
            if matches_ignore_patterns(path=filepath, directory=directory):
                continue
            checksum = calculate_checksum(filepath)
            if checksum:
                WATCHED_FILES[filepath] = WatchState(checksum, None)
    if DEBUG:
        click.secho("Scanning complete.", fg="green")


def resolve_sweep_interval(cli_value: float | None, selected_server: dict) -> float:
    """Resolve the watch sweep interval: CLI flag, then project config, then default.

    Values below MIN_SWEEP_INTERVAL and non-finite values (NaN, Infinity) are
    rejected: an accidental 0 would turn the sweep into a per-cycle full-tree
    hash, and a non-finite value would silently disable it. A CLI value that
    is invalid raises click.BadParameter; an invalid config value falls back
    to the default with a warning.
    """
    if cli_value is not None:
        if not math.isfinite(cli_value):
            raise click.BadParameter("--sweep-interval must be a finite number of seconds.")
        if cli_value < MIN_SWEEP_INTERVAL:
            raise click.BadParameter(f"--sweep-interval must be at least {MIN_SWEEP_INTERVAL:g} seconds.")
        return cli_value
    config_value = selected_server.get("sweep_interval")
    if config_value is not None:
        try:
            interval = float(config_value)
        except (TypeError, ValueError):
            click.secho(
                f"Invalid sweep_interval in config: {config_value!r}; using the default of {WATCH_SWEEP_INTERVAL:g} seconds.",
                fg="yellow",
            )
            return WATCH_SWEEP_INTERVAL
        if not math.isfinite(interval):
            click.secho(
                f"sweep_interval in config must be a finite number of seconds; using the default of {WATCH_SWEEP_INTERVAL:g} seconds.",
                fg="yellow",
            )
            return WATCH_SWEEP_INTERVAL
        if interval < MIN_SWEEP_INTERVAL:
            click.secho(
                f"sweep_interval in config must be at least {MIN_SWEEP_INTERVAL:g} seconds; using the default of {WATCH_SWEEP_INTERVAL:g} seconds.",
                fg="yellow",
            )
            return WATCH_SWEEP_INTERVAL
        return interval
    return WATCH_SWEEP_INTERVAL


def sweep_directory(directory: str) -> tuple[list[str], list[str]]:
    """Re-scan the tree to catch events the observer missed.

    Every file is re-hashed and reported as dirty when its content differs
    from what the server has; tracked files that disappeared are reported as
    deleted. Returns (dirty_paths, deleted_paths).
    """
    dirty = []
    deleted = []
    seen = set()
    for current_directory, subdirectories, files in os.walk(directory):
        subdirectories[:] = [d for d in subdirectories if d not in EXCLUDED_DIRECTORIES]
        for file in files:
            filepath = os.path.join(current_directory, file)
            if matches_ignore_patterns(path=filepath, directory=directory):
                continue
            seen.add(filepath)
            if refresh_path_state(filepath):
                dirty.append(filepath)
    for path in list(WATCHED_FILES):
        if path not in seen:
            WATCHED_FILES.pop(path, None)
            RETRY_QUEUE.pop(path, None)
            deleted.append(path)
    return dirty, deleted


def read_ignore_file(path: str) -> list[str]:
    with open(path, encoding="utf-8") as file:
        return [line.rstrip("\r\n") for line in file]


def _translate_nested_gitignore_pattern(pattern: str, relative_directory: str) -> str:
    """Translate one pattern from a nested .gitignore to be relative to the
    package root, keeping git's semantics: a pattern without a slash matches
    at any depth below the .gitignore's own directory, while an anchored one
    (leading slash or containing slash) is fixed to that directory. Negation
    and directory-only patterns pass through unchanged.
    """
    prefix = ""
    if pattern.startswith("!"):
        prefix = "!"
        pattern = pattern[1:]
    if not pattern or pattern.startswith("#"):
        return prefix + pattern
    dir_only = pattern.endswith("/")
    if dir_only:
        pattern = pattern[:-1]
    if pattern.startswith("/"):
        translated = relative_directory + pattern
    elif pattern.startswith("**/") or "/" in pattern:
        translated = relative_directory + "/" + pattern
    else:
        translated = relative_directory + "/**/" + pattern
    return prefix + translated + ("/" if dir_only else "")


def _gitignored_directory(matcher, parent_directory: str, subdirectory: str, directory: str) -> bool:
    """Return True if git would not descend from `parent_directory` into
    `subdirectory` because the patterns compiled into `matcher` (the root
    .gitignore and the ancestors' nested ones) exclude the directory."""
    relative = os.path.relpath(os.path.join(parent_directory, subdirectory), directory).replace(os.sep, "/")
    return bool(matcher.match(relative, is_dir=True))


def nested_gitignore_patterns(directory: str) -> list[str]:
    """Collect patterns from .gitignore files below `directory` (excluding the
    root one), translated to be relative to `directory`.

    The archive builder excludes files git ignores — nested .gitignore files
    included, via `git ls-files -i -o` — so the watcher must ignore the same
    files. A file the watcher tracks but the archive can never include stays
    dirty forever and keeps triggering full installs until the archive-skip
    suspension kicks in.

    Directories the root ignore list (the root .gitignore, or the built-in
    defaults when there is none) or an ancestor .gitignore excludes are not
    descended into, mirroring git, which never reads the .gitignore files
    inside an excluded directory: a negation there must not un-ignore
    anything, because the archive can never include files from an excluded
    directory.
    """
    root_gitignore = os.path.abspath(os.path.join(directory, ".gitignore"))
    if os.path.exists(root_gitignore):
        root_patterns = read_ignore_file(root_gitignore)
    else:
        # No root .gitignore: use the same default ignore list as
        # load_ignore_patterns, so directories git ignores by default are not
        # descended into either.
        root_patterns = GITIGNORE.split("\n")
    patterns: list[str] = []
    matcher = gitmatch.compile(root_patterns)
    for current_directory, subdirectories, files in os.walk(directory):
        gitignore_path = os.path.abspath(os.path.join(current_directory, ".gitignore"))
        if gitignore_path != root_gitignore and ".gitignore" in files:
            relative_directory = os.path.relpath(current_directory, directory).replace(os.sep, "/")
            translated = [
                _translate_nested_gitignore_pattern(pattern, relative_directory)
                for pattern in read_ignore_file(gitignore_path)
            ]
            patterns.extend(translated)
            if translated:
                matcher = gitmatch.compile(root_patterns + patterns)
        subdirectories[:] = [
            d
            for d in subdirectories
            if d not in EXCLUDED_DIRECTORIES and not _gitignored_directory(matcher, current_directory, d, directory)
        ]
    return patterns


def load_ignore_patterns(directory: str) -> list[str]:
    gitignore_path = os.path.join(directory, ".gitignore")
    if os.path.exists(gitignore_path):
        ignore_patterns = read_ignore_file(gitignore_path)
    else:
        ignore_patterns = GITIGNORE.split("\n")

    ignore_patterns.extend(nested_gitignore_patterns(directory))

    watch_ignore_path = os.path.join(directory, WATCH_IGNORE_FILE)
    if os.path.exists(watch_ignore_path):
        ignore_patterns.extend(read_ignore_file(watch_ignore_path))

    ignore_patterns.extend([".git/", ".gitignore", WATCH_IGNORE_FILE])
    return ignore_patterns


def matches_ignore_patterns(path: str, directory: str) -> bool:
    global WATCH_IGNORE_MTIME, GITIGNORE_MTIME, GITMATCH_COMPILED, GITMATCH_DIRECTORY
    if is_archive_excluded(os.path.basename(path)):
        return True
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


def invalidate_ignore_cache() -> None:
    """Drop the compiled ignore matcher; the next match recompiles it.

    Needed when a nested .gitignore changes: only the root .gitignore's mtime
    is part of the cache key in matches_ignore_patterns, so a nested one would
    otherwise never invalidate the compiled patterns.
    """
    global GITMATCH_COMPILED
    GITMATCH_COMPILED = None


class WatchHandler(FileSystemEventHandler):
    def __init__(self, *args, **kwargs):
        self.directory = kwargs.pop("directory")
        super().__init__(*args, **kwargs)

    def on_any_event(self, event):
        event_type = getattr(event, "event_type", None)
        if event_type in ("opened", "closed") or (event.is_directory and event_type == "modified"):
            return
        if event.is_directory:
            return
        event_path = os.path.abspath(event.src_path)
        if os.path.basename(event_path) == ".gitignore":
            # Nested .gitignore files are collected when the ignore matcher
            # is compiled, so any change to one must invalidate the cache.
            invalidate_ignore_cache()
        if matches_ignore_patterns(path=event_path.replace("\\", "/"), directory=self.directory):
            return
        if event_type not in ("created", "modified", "deleted"):
            return

        with LAST_MODIFIED_LOCK:
            event_bucket = LAST_MODIFIED["files"].setdefault(event_path, {})
            if event_type == "deleted":
                LAST_MODIFIED["files"][event_path] = {"deleted": True}
            else:
                if "deleted" in event_bucket:
                    event_bucket = {}
                event_bucket[event_type] = True
                LAST_MODIFIED["files"][event_path] = event_bucket
            LAST_MODIFIED["time"] = time.time()
            if event_path.endswith(".py") and event_path.startswith(
                os.path.join(self.directory, "docassemble") + os.sep
            ):
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
@click.option("--dry-run", is_flag=True, help="Show what watch would install without uploading anything.")
@click.option("--show-files", is_flag=True, help="With --dry-run, list the files that would be uploaded.")
@click.option(
    "--sweep-interval",
    metavar="SECONDS",
    type=float,
    default=None,
    help="How often to re-scan the package directory for changes the file-system observer missed (default: 5 minutes). Can also be set with the sweep_interval key in the watch section of the project config.",
)
def watch(
    directory,
    config,
    project_config,
    api,
    server,
    playground,
    no_playground,
    restart,
    buffer,
    dry_run,
    show_files,
    sweep_interval=None,
):
    """
    Watch a package directory and `install` any changes. Press Ctrl + c to exit.

    If the --directory option is not specified, `watch` will look for a directory entry in the config file. The corresponding server entry will be selected automatically if the "directory" key in the config file matches the directory being watched. If a match is found, the "playground" key in the config file will be used if it exists and if no --playground option was specified.
    """
    selected_server = resolve_command_server_with_cleanup("watch", directory, config, api, server, project_config)
    restart_param = restart
    scan_directory(directory)
    global LAST_MODIFIED
    event_handler = WatchHandler(directory=directory)
    observer = Observer()
    observer.schedule(event_handler, directory, recursive=True)
    observer.start()
    click.echo()
    click.echo(f"""Server: {selected_server["name"]}""")

    if no_playground or playground == "":
        playground = ""
        click.echo("Location: Package")
    elif playground is None:
        if (
            project_config
            and "playground" in selected_server
            or (
                "directory" in selected_server
                and selected_server["directory"] == directory
                and "playground" in selected_server
            )
        ):
            playground = selected_server["playground"]
        else:
            click.echo("Location: Package")
    if playground:
        click.echo(f"""Location: Playground "{playground}" """)

    stop_message = """\nStopping "docassemblecli3 watch"."""
    announced_conflicts: frozenset[str] = frozenset()
    try:
        if playground:
            conflict_candidates = _readable_paths(list(WATCHED_FILES))
            conflicts = playground_name_conflicts(conflict_candidates)
            if conflicts:
                # The Playground stores files flat by name, so conflicting
                # files can not both be synced; warn once and skip them
                # (package installs still include them).
                announced_conflicts = frozenset(playground_conflict_paths(conflict_candidates))
                click.secho(format_playground_conflict_skip(conflicts), fg="yellow")
        if "startup" in selected_server and selected_server["startup"] == "install":
            if dry_run:
                click.secho("""Previewing startup install.""", fg="cyan")
            else:
                click.secho("""Installing on startup.""", fg="cyan")
            try:
                startup_map = package_installer(
                    directory=directory,
                    apiurl=selected_server["apiurl"],
                    apikey=selected_server["apikey"],
                    playground=playground,
                    restart=restart,
                    dry_run=dry_run,
                    show_files=show_files,
                    announced_conflicts=announced_conflicts,
                )
                if dry_run:
                    for state in WATCHED_FILES.values():
                        state.uploaded_hash = state.checksum
                else:
                    mark_archive_uploaded(startup_map, directory, playground)
            except DaCliError as err:
                click.secho(f"\n{err}\n", fg="red")
            except Exception as exc:  # noqa: BLE001
                click.secho(f"\n{exc}\n", fg="red")
            click.echo("")

        click.echo(f"""Watching: {directory}""")
        click.secho(f"""[{datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")}] Started""", fg="green")

        sweep_interval = resolve_sweep_interval(sweep_interval, selected_server)
        last_sweep = None
        while True:
            now_monotonic = time.monotonic()
            if last_sweep is None or now_monotonic - last_sweep >= sweep_interval:
                sweep_dirty, sweep_deleted = sweep_directory(directory)
                if playground and not dry_run:
                    playground_reconcile(
                        apiurl=selected_server["apiurl"],
                        apikey=selected_server["apikey"],
                        playground=playground,
                    )
                last_sweep = now_monotonic
            else:
                sweep_dirty, sweep_deleted = [], []
            events = None
            should_restart = False
            with LAST_MODIFIED_LOCK:
                if LAST_MODIFIED["time"] and time.time() - LAST_MODIFIED["time"] >= WATCH_SETTLE_DELAY:
                    events = LAST_MODIFIED["files"]
                    should_restart = LAST_MODIFIED["restart"]
                    LAST_MODIFIED = {"time": 0, "files": {}, "restart": False}
            dirty = list(sweep_dirty)
            deleted = list(sweep_deleted)
            if events:
                event_dirty, event_deleted = handle_watch_events(events)
                dirty.extend(event_dirty)
                deleted.extend(event_deleted)
                should_restart = should_restart or any(path.endswith(".py") for path in event_dirty)
            now_monotonic = time.monotonic()
            for path, (due_at, _delay) in list(RETRY_QUEUE.items()):
                if due_at <= now_monotonic:
                    if refresh_path_state(path):
                        dirty.append(path)
                    del RETRY_QUEUE[path]
            for path, (due_at, _delay) in list(PENDING_DELETIONS.items()):
                if due_at <= now_monotonic:
                    del PENDING_DELETIONS[path]
                    if not os.path.exists(path):
                        deleted.append(path)
            dirty = list(dict.fromkeys(dirty))
            deleted = list(dict.fromkeys(deleted))
            if playground and dirty:
                # A rename or new file can create a name conflict mid-session.
                # The conflicting files can not both exist in the flat
                # Playground layout, so skip them and sync everything else;
                # warn only when the set of conflicts changes. Files that can
                # not be read are not conflict participants (they can never
                # be uploaded), so their readable same-named siblings sync.
                conflict_candidates = _readable_paths(list(WATCHED_FILES))
                conflicted_paths = playground_conflict_paths(conflict_candidates)
                if frozenset(conflicted_paths) != announced_conflicts:
                    announced_conflicts = frozenset(conflicted_paths)
                    if announced_conflicts:
                        click.secho(
                            format_playground_conflict_skip(playground_name_conflicts(conflict_candidates)),
                            fg="yellow",
                        )
                # A file whose Playground name has an unconfirmed delete pending
                # must not upload: the retried delete would remove the copy it
                # just wrote. Defer it until the delete is confirmed.
                pending_names = {server_file_name(path) for path in PENDING_DELETIONS}
                dirty = [
                    path
                    for path in dirty
                    if path not in conflicted_paths and server_file_name(path) not in pending_names
                ]
            if not dirty and not deleted:
                time.sleep(0.2)
                continue
            if dry_run:
                click.secho(
                    f"""[{datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")}] Dry run: previewing install...""",
                    fg="cyan",
                )
            else:
                click.secho(
                    f"""[{datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")}] Installing...""",
                    fg="yellow",
                )
            if restart_param == "yes" or (restart_param == "auto" and should_restart):
                effective_restart = "yes"
                time.sleep(buffer)
            else:
                effective_restart = "no"
            for item in dirty + deleted:
                click.echo("  " + item.replace(directory, ""))
            try:
                if playground:
                    if deleted and not dry_run:
                        failed_deletes = playground_delete_files(
                            apiurl=selected_server["apiurl"],
                            apikey=selected_server["apikey"],
                            playground=playground,
                            paths=deleted,
                        )
                        backoff_paths(failed_deletes, PENDING_DELETIONS)
                    if dirty:
                        uploads = classify_playground_paths(dirty)
                        if uploads is None:
                            archive_map = package_installer(
                                directory=directory,
                                apiurl=selected_server["apiurl"],
                                apikey=selected_server["apikey"],
                                playground=playground,
                                restart=effective_restart,
                                dry_run=dry_run,
                                show_files=show_files,
                                announced_conflicts=announced_conflicts,
                            )
                            if dry_run:
                                mark_previewed(dirty)
                            else:
                                mark_archive_uploaded(archive_map, directory, playground)
                                schedule_unconfirmed_retries(dirty, archive_map, directory)
                        elif dry_run:
                            show_dry_run_playground_upload(playground, uploads, show_files=show_files)
                            click.secho("Dry run: incremental Playground upload preview complete.", fg="cyan")
                            mark_previewed(dirty)
                        else:
                            sent_hashes, failed = playground_upload_batch(
                                apiurl=selected_server["apiurl"],
                                apikey=selected_server["apikey"],
                                playground=playground,
                                dirty_paths=dirty,
                            )
                            for path, sent_hash in sent_hashes.items():
                                mark_uploaded(path, sent_hash)
                            backoff_paths(failed, RETRY_QUEUE)
                            if sent_hashes and not failed:
                                announce_installed()
                else:
                    if dirty:
                        archive_map = package_installer(
                            directory=directory,
                            apiurl=selected_server["apiurl"],
                            apikey=selected_server["apikey"],
                            playground=playground,
                            restart=effective_restart,
                            dry_run=dry_run,
                            show_files=show_files,
                        )
                        if dry_run:
                            mark_previewed(dirty)
                        else:
                            mark_archive_uploaded(archive_map, directory, None)
                            schedule_unconfirmed_retries(dirty, archive_map, directory)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                click.secho(f"\n{exc}\n", fg="red")
                backoff_paths(dirty, RETRY_QUEUE)
                backoff_paths(deleted, PENDING_DELETIONS)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
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
            + str(datetime.datetime.now(tz=datetime.UTC).year)
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
description = {description!r}
readme = "README.md"
requires-python = ">=3.12"
authors = [
    {{ name = {developer_name!r}, email = {developer_email!r} }},
]
dependencies = []

[project.urls]
Homepage = {package_url!r}

[tool.setuptools.packages.find]
where = ["."]
"""
    if license:
        pyproject += f'\nlicense = {license!r}\nlicense-files = ["LICENSE"]\n'
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
@click.option(
    "--api",
    "-a",
    type=(APIURLType(), str),
    default=(None, None),
    help="URL of the docassemble server and API key of the user (admin or developer)",
)
@click.option("--config", "config_path", "-c", type=click.Path(), help="Specify the global config file to use")
@click.option(
    "--project-config", "use_project_config", is_flag=True, help="Add to the package-local .docassemblecli file"
)
@click.option("--global-config", "use_global_config", is_flag=True, help="Add to the global config file")
@click.option("--directory", "-d", type=click.Path(), help="Associate the server with this package directory")
@click.option(
    "--playground",
    "-p",
    metavar="(PROJECT)",
    is_flag=False,
    flag_value="default",
    help="Set the default Playground or specify the default Playground project.",
)
@click.option(
    "--no-playground",
    is_flag=True,
    help="Do not set a default Playground for this server.",
)
@click.option(
    "--install-default/--no-install-default",
    default=None,
    help="In the local project config, set this server as the default for install.",
)
@click.option(
    "--install-playground",
    metavar="(PROJECT)",
    is_flag=False,
    flag_value="default",
    default=None,
    help="In the local project config, set the default Playground for install.",
)
@click.option(
    "--watch-default/--no-watch-default",
    default=None,
    help="In the local project config, set this server as the default for watch.",
)
@click.option(
    "--watch-playground",
    metavar="(PROJECT)",
    is_flag=False,
    flag_value="default",
    default=None,
    help="In the local project config, set the default Playground for watch.",
)
@click.option(
    "--watch-startup",
    type=click.Choice(["install", "none"], case_sensitive=False),
    default=None,
    help="In the local project config, control whether watch installs once on startup.",
)
def add(
    config_path,
    api,
    use_project_config,
    use_global_config,
    directory,
    playground,
    no_playground,
    install_default,
    install_playground,
    watch_default,
    watch_playground,
    watch_startup,
):
    """
    Add a server to the config file.
    """
    apiurl, apikey = api
    local_command_options_used = any(
        value is not None
        for value in (install_default, install_playground, watch_default, watch_playground, watch_startup)
    )
    if local_command_options_used and not (use_project_config or use_global_config or config_path):
        use_project_config = True
    target_scope, cfg, env, sections = resolve_config_target(
        config_path=config_path,
        use_project_config=use_project_config,
        use_global_config=use_global_config,
        directory=directory,
    )
    if install_default is False and install_playground is not None:
        raise click.BadParameter(
            "Cannot be combined with --no-install-default.",
            param_hint="--install-playground",
        )
    if watch_default is False and (watch_playground is not None or watch_startup is not None):
        raise click.BadParameter(
            "Cannot be combined with --no-watch-default.",
            param_hint="--watch-playground",
        )
    if target_scope != "local" and local_command_options_used:
        raise click.BadParameter(
            "Install/watch defaults can only be set in the package-local project config.",
            param_hint="--project-config",
        )

    if target_scope == "local":
        stored_directory = validate_package_directory(None, None, directory) if directory is not None else None
    else:
        stored_directory = (
            validate_package_directory(None, None, directory)
            if directory is not None
            else prompt_for_optional_directory()
        )

    if no_playground:
        playground = ""
    elif playground is None:
        playground = prompt_for_optional_playground()

    if target_scope == "local":
        configure_install = install_default if install_default is not None else bool(install_playground is not None)
        if install_default is None and install_playground is None:
            configure_install = prompt_for_command_default("install")
        if configure_install and install_playground is None:
            install_playground = prompt_for_command_playground("install")

        configure_watch = (
            watch_default
            if watch_default is not None
            else bool(watch_playground is not None or watch_startup is not None)
        )
        if watch_default is None and watch_playground is None and watch_startup is None:
            configure_watch = prompt_for_command_default("watch")
        if configure_watch and watch_playground is None:
            watch_playground = prompt_for_command_playground("watch")
        if configure_watch and watch_startup is None:
            watch_startup = prompt_for_watch_startup()
        if watch_startup == "none":
            watch_startup = None

    apiurl, apikey = ensure_api_credentials(apiurl=apiurl, apikey=apikey)
    server_name = name_from_url(apiurl)

    if target_scope == "local":
        env = add_server_to_env(
            cfg=None,
            env=env,
            apiurl=apiurl,
            apikey=apikey,
            directory=stored_directory,
            playground=playground,
            validate_api=False,
        )
        sections = apply_project_command_defaults(
            sections=sections or {"install": {}, "watch": {}},
            server_name=server_name,
            configure_install=configure_install,
            install_playground=install_playground,
            configure_watch=configure_watch,
            watch_playground=watch_playground,
            watch_startup=watch_startup,
        )
        if save_project_config(cfg, env, sections):
            click.echo(f"Configuration saved: {cfg}")
    else:
        add_server_to_env(
            cfg=cfg,
            env=env,
            apiurl=apiurl,
            apikey=apikey,
            directory=stored_directory,
            playground=playground,
            validate_api=False,
        )


@config.command(context_settings=CONTEXT_SETTINGS)
@click.option("--config", "config_path", "-c", type=click.Path(), help="Specify the global config file to use")
@click.option("--project-config", "use_project_config", is_flag=True, help="Use the package-local .docassemblecli file")
@click.option("--global-config", "use_global_config", is_flag=True, help="Use the global config file")
@click.option("--directory", "-d", type=click.Path(), help="Package directory that contains the local project config")
@click.option("--server", "-s", metavar="SERVER", help="Specify a server to remove from the config file")
def remove(config_path, use_project_config, use_global_config, directory, server):
    """
    Remove a server from the config file.
    """
    target_scope, cfg, env, sections = resolve_config_target(
        config_path=config_path,
        use_project_config=use_project_config,
        use_global_config=use_global_config,
        directory=directory,
    )
    if not server:
        click.echo(f"""Servers in {cfg}:""")
        for item in display_servers(env=env):
            click.echo("  " + item)
        server = click.prompt("Remove which server?")
    selected_server = select_server(cfg=cfg, env=env, server=server)
    env.remove(selected_server)
    if target_scope == "local":
        save_project_config(cfg, env, sections or {"install": {}, "watch": {}})
    else:
        save_config(cfg=cfg, env=env)
    click.echo(f"""Server "{server}" has been removed from {cfg}.""")


@config.command(name="show", context_settings=CONTEXT_SETTINGS)
@click.option("--config", "config_path", "-c", type=click.Path(), help="Specify the global config file to use")
@click.option("--project-config", "use_project_config", is_flag=True, help="Use the package-local .docassemblecli file")
@click.option("--global-config", "use_global_config", is_flag=True, help="Use the global config file")
@click.option("--directory", "-d", type=click.Path(), help="Package directory that contains the local project config")
def show(config_path, use_project_config, use_global_config, directory):
    """
    Show the servers in the config file.
    """
    _, _, env, sections = resolve_config_target(
        config_path=config_path,
        use_project_config=use_project_config,
        use_global_config=use_global_config,
        directory=directory,
    )
    for item in display_servers(env=env):
        click.echo("  " + item)
    for item in display_project_command_sections(sections):
        click.echo("  " + item)


@config.command(context_settings=CONTEXT_SETTINGS)
@click.argument("config", type=click.Path())
def new(config):
    """
    Create a new config file.
    """
    config_path = os.path.abspath(config)
    if os.path.exists(config_path) and os.stat(config_path).st_size != 0:
        raise click.BadParameter("File exists and is not empty!")
    env = []
    try:
        with open(config_path, "w", encoding="utf-8") as fp:
            yaml.dump(env, fp)
        os.chmod(config_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        raise click.BadParameter("File is not usable.")
    click.echo(f"""Config created successfully: {config_path}""")
    if click.confirm("Do you want to add a server to this new config file?", default=True):
        apiurl, apikey = prompt_for_api()
        add_server_to_env(cfg=config_path, env=env, apiurl=apiurl, apikey=apikey)


@config.command(context_settings=CONTEXT_SETTINGS, hidden=True)
@common_params_for_config
@common_params_for_api
def server_version(config, api, server):
    selected_server = select_server(*config, *api, server)
    try:
        r = http_get(
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
    except requests.exceptions.RequestException as err:
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
