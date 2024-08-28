# docassemblecli3

`docassemblecli3` provides command-line utilities for interacting with
[docassemble] servers. This package is meant to be installed on your local
machine, not on a [docassemble] server.

This project is based on [docassemblecli] by Jonathan Pyle Copyright (c) 2021
released under the MIT License.

## Differences from [docassemblecli]

- Requires Python 3.
- Adds multi-platform file monitoring, a.k.a. `dawatchinstall` works on Windows
  and without requiring fswatch.
- Adds queueing and batching to improve file monitoring and installation
  (improves multi-file saving, late file metadata changes, and avoids server
  restart induced timeouts).
- Improves invocation, requiring less configuration of PATH and scripts to work,
  especially in Windows (and does not conflict with [docassemblecli]).
- Improved command structure and option flags (so please read this documentation
  or utilize the `--help` or `-h` options in the terminal).

## Prerequisites

This program should only require that you have Python 3.8 installed on your
computer, but it was developed and tested with Python 3.12. Please report any
bugs or errors you experience.

## Installation

To install `docassemblecli3` from PyPI, run:

    pip install docassemblecli3

## Usage

`docassemblecli3` be more easily be run by typing `da`.

All of the command options, such as showing the "help", have both long `--help`
and short `-h` versions. This documentation will always use the long version,
but feel free to use whichever you prefer.

    Usage: da [OPTIONS] COMMAND [ARGS]...

    Commands for interacting with docassemble servers.

    Options:
    -C, --color / -N, --no-color  Overrides color auto-detection in interactive
                                    terminals.
    -h, --help                    Show this message and exit.

    Commands:
    config   Manage servers in a docassemblecli config file.
    create   Create an empty docassemble add-on package.
    install  Install a docassemble package on a docassemble server.
    watch    Watch a package directory and `install` any changes.

### create

`docassemblecli3` provides a command-line utility called `create`, which
creates an empty **docassemble** add-on package.

To create a package called `docassemble-foobar` in the current directory, run:

    da create --package foobar

You will be asked some questions about the package and the developer. This
information is necessary because it goes into the `setup.py`, `README.md`, and
`LICENSE` files of the package. If you do not yet know what answers to give,
just press enter, and you can edit these files later.

When the command exits, you will find a directory in the current directory
called `docassemble-foobar` containing a shell of a **docassemble** add-on
package.

You can run `da create --help` to get more information about how `create`
works:

    Usage: da create [OPTIONS]

    Create an empty docassemble add-on package.

    Options:
    --package PACKAGE          Name of the package you want to create
    --developer-name NAME      Name of the developer of the package
    --developer-email EMAIL    Email of the developer of the package
    --description DESCRIPTION  Description of package
    --url URL                  URL of package
    --license LICENSE          License of package
    --version VERSION          Version number of package
    --output OUTPUT            Output directory in which to create the package
    -h, --help                 Show this message and exit.

### install

`docassemblecli3` provides a command-line utility called `install`, which
installs a Python package on a remote server using files on your local computer.

For example, suppose that you wrote a docassemble extension package called
`docassemble.foobar` using the **docassemble** Playground. In the Playground,
you can download the package as a ZIP file called `docassemble-foobar.zip`. You
can then unpack this ZIP file and you will see a directory called
`docassemble-foobar`. Inside of this directory there is a directory called
`docassemble` and a `setup.py` file.

From the command line, use `cd` to navigate into the directory
`docassemble-foobar`. Then run:

    da install

or you can specify the directory of the package you want to install (if
`docassemble-foobar` is in your current directory):

    da install --directory docassemble-foobar

The first time you run this command, it will ask you for the URL of your
**docassemble** server and the [API key] of a user with `admin` or `developer`
privileges.

It will look something like this:

    $ da install --directory docassemble-foobar
    Base URL of your docassemble server (e.g., https://da.example.com): https://da.example.com
    API key of admin or developer user on https://da.example.com: H3PWMKJOIVAXL4PWUJH3HG7EKPFU5GYT
    Testing the URL and API key...
    Success!
    Configuration saved: ~\.docassemblecli
    [2024-08-16 18:10:18] Installing...
    Server will restart.
    Waiting for package to install...
    Waiting for server...
    [2024-08-16 18:11:43] Installed.

The next time you run `da install`, it will not ask you for the URL and API key.

You can run `da install --help` to get more information about how `install`
works:

    Usage: da install [OPTIONS]

    Install a docassemble package on a docassemble server.

    `da install` tries to get API info from the --api option first (if used), then
    from the first server listed in the ~/.docassemblecli file if it exists
    (unless the --config option is used), then it tries to use environmental
    variables, and finally it prompts the user directly.

    Options:
    -a, --api <URL TEXT>...      URL of the docassemble server and API key of
                                the user (admin or developer)
    -s, --server SERVER          Specify a server from the config file
    -d, --directory PATH         Specify package directory [default: current
                                directory]
    -c, --config PATH            Specify the config file to use or leave it
                                blank to skip using any config file  [default:
                                C:\Users\jacka\.docassemblecli]
    -p, --playground (PROJECT)   Install into the default Playground or into the
                                specified Playground project.
    -r, --restart [yes|no|auto]  On package install: yes, force a restart | no,
                                do not restart | auto, only restart if the
                                package has any .py files or if there are
                                dependencies to be installed  [default: auto]
    -h, --help                   Show this message and exit.

For example, you might want to pass the URL and API key in the command itself:

    da install --api https://da.example.com H3PWMKJOIVAXL4PWUJH3HG7EKPFU5GYT --directory docassemble-foobar

If you have more than one server, you can utilize one of the `config` tools `add`:

    da config add

to add an additional server configuration to store in your `.docassemblecli`
config file. Then you can select the server using `--server`:

    da install --server da.example.com --directory docassemble-foobar

If you do not specify a `--server`, the first server indicated in your
`.docassemblecli` file will be used.

The `--restart no` option can be used when your **docassemble** installation
only uses one server (which is typical) and you are not modifying .py files. In
this case, it is not necessary for the Python web application to restart after
the package has been installed. This will cause `da install` to return a few
seconds faster than otherwise.

The `--restart yes` option should be used when you want to make sure that
**docassemble** restarts the Python web application after the package is
installed. By default, `da install` will avoid restarting the server if the
package has no module files and all of its dependencies (if any) are installed.

By default, `da install` installs a package on the server. If you want to install
a package into your Playground, you can use the `--playground` option.

    da install --playground --directory docassemble-foobar

If you want to install into a particular project in your Playground, indicate
the project after the `--playground` option, for example project "testing".

    da install --playground testing --directory docassemble-foobar

Installing into the Playground with `--playground` is faster than installing an
actual Python package because it does not need to run `pip`.

If your development installation uses more than one server, it is safe to run
`da install --playground` with `--restart no` if you are only changing YAML files,
because Playground YAML files are stored in cloud storage and will thus be
available immediately to all servers.

### watch

You can use `watch` to automatically `install` your docassemble package every
time a file in your package directory is changed.

For example, if you run:

    da watch --playground testing --directory docassemble-foobar

This will monitor the `docassemble-foobar` directory, and if any non-`.py` file
changes, it will run:

    da install --playground testing --restart no --directory docassemble-foobar

If a `.py` file is changed, however, it will run

    da install --playground testing --restart yes --directory docassemble-foobar

With `da watch --playground` constantly running, soon after you save a YAML file
on your local machine, it will very quickly be available for testing on your
server.

To exit `watch`, press **Ctrl + c**.

You can run `da watch --help` to get more information about how `watch`
works:

    Usage: da watch [OPTIONS]

    Watch a package directory and `install` any changes. Press Ctrl + c to exit.

    Options:
    -d, --directory PATH         Specify package directory [default: current
                                directory]
    -c, --config PATH            Specify the config file to use or leave it
                                blank to skip using any config file  [default:
                                C:\Users\jacka\.docassemblecli]
    -p, --playground (PROJECT)   Install into the default Playground or into the
                                specified Playground project.
    -a, --api <URL TEXT>...      URL of the docassemble server and API key of
                                the user (admin or developer)
    -s, --server SERVER          Specify a server from the config file
    -r, --restart [yes|no|auto]  On package install: yes, force a restart | no,
                                do not restart | auto, only restart if any .py
                                files were changed  [default: auto]
    -b, --buffer SECONDS         (On server restart only) Set the buffer (wait
                                time) between a file change event and package
                                installation. If you are experiencing multiple
                                installs back-to-back, try increasing this
                                value.  [default: 3]
    -h, --help                   Show this message and exit.

Your package's `.gitignore` file is also used by `watch` to decide which files
to ignore. If you don't have a `.gitignore` file in your package, then the
default `.gitignore` that `create` makes is used instead. The `.git/` directory
and `.gitignore` file are both also ignored by `watch` (note: don't add them to
your `.gitignore`).

#### watchdog

The `watch` command now depends on the
[watchdog](https://pypi.org/project/watchdog/) Python package. This allows
`watch` to work on the following platforms that [watchdog] supports:

- Linux 2.6 (inotify)
- macOS (FSEvents, kqueue)
- FreeBSD/BSD (kqueue)
- Windows (ReadDirectoryChangesW with I/O completion ports;
  ReadDirectoryChangesW worker threads)
- OS-independent (polling the disk for directory snapshots and comparing them
  periodically; slow and not recommended)

An additional note from [watchdog]'s documentation:

Note that when using watchdog with kqueue (macOS and BSD), you need the number of file
descriptors allowed to be opened by programs running on your system to be
increased to more than the number of files that you will be monitoring. The
easiest way to do that is to edit your ~/.profile file and add a line similar
to:

```bash
ulimit -n 1024
```

This is an inherent problem with kqueue because it uses file descriptors to
monitor files. That plus the enormous amount of bookkeeping that watchdog needs
to do in order to monitor file descriptors just makes this a painful way to
monitor files and directories. In essence, kqueue is not a very scalable way to
monitor a deeply nested directory of files and directories with a large number
of files.

### config

There are four commands for managing your saved servers/your config file, `add`,
`display`, `new`, and `remove`.

    Usage: da config [OPTIONS] COMMAND [ARGS]...

    Manage servers in a docassemblecli config file.

    Options:
    -h, --help  Show this message and exit.

    Commands:
    add      Add a server to the config file.
    display  List the servers in the config file.
    new      Create a new config file.
    remove   Remove a server from the config file.

They are all really easy to use and will prompt you for all necessary
information.

## How it works

The `install` command is just a simple Python script that creates a ZIP file and
uploads it through the **docassemble** API. Feel free to copy the code and write
your own scripts to save yourself time. (That's how this version started!)

## Contributing

Pull requests are welcome. For major changes, please open an issue first
to discuss what you would like to change.

Please make sure to update tests as appropriate.

## License

[MIT](https://choosealicense.com/licenses/mit/)

[docassemble]: https://docassemble.org
[docassemblecli]: https://github.com/jhpyle/docassemblecli/
[API key]: https://docassemble.org/docs/api.html#manage_api
[watchdog]: https://pypi.org/project/watchdog/