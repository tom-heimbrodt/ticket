# Ticket

A portable command-line ticket tracker written in Python. Store one YAML file
per ticket, configure workflows and relationships, search tickets with fuzzy
matching, and export results as JSON or YAML.

Requires Python 3.9+ on Windows, macOS, or Linux and PyYAML. Git is optional;
when available, `git config user.name` supplies default authors and assignees.

## Run or install

Install the command from this directory with uv:

```sh
uv tool install .
ticket --help
```

uv creates an isolated environment and installs the `ticket` command, including
PyYAML. This works with externally managed system Python installations such as
Arch Linux's. If uv reports that its executable directory is missing from
`PATH`, run `uv tool update-shell` and restart your shell.

If another executable named `ticket` is already installed, uv may report
`Executable already exists: ticket`. To replace that command, run
`uv tool install --force .`. Save a copy of the existing executable first
if you want to keep it available.

For development, use `uv tool install --editable .` so source edits take effect
without reinstalling. To refresh a regular local installation after changes,
run `uv tool install --reinstall .`. To remove it, use
`uv tool uninstall ticket-cli` (the package name differs from the command).

Alternatively, use `pipx install .`, or run the script in a virtual environment:

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python ticket.py --help
```

On Windows, use `.venv\Scripts\python.exe` instead of `.venv/bin/python`.
On systems where Python is named `python3`, use that instead of `python`.
If copying only `ticket.py`, install `PyYAML>=6,<7` inside that virtual
environment. See the [uv tools documentation](https://docs.astral.sh/uv/guides/tools/)
for more about isolated command installation.

## Use

```sh
ticket create bug "Crash on startup" "Steps to reproduce..." --overwrite-id bug-startup
ticket ls
ticket show bug-startup
ticket modify bug-startup status=in-progress priority=2
ticket tag bug-startup urgent
ticket comment bug-startup --author Alice "Reproduced on Windows"
ticket search startup --fields summary,description --min-score 0.8
ticket ls type=bug --sort new --limit 0 --output json
ticket verify --output json
ticket overview
```

With no command, `ticket` prints the overview. `--help` is available for each
command. `create` takes a type, summary, and optional description. `modify`
accepts `FIELD=VALUE` assignments and repeatable `--link ID:RELATIONSHIP`,
`--unlink ID[:RELATIONSHIP]`, `--tag`, `--untag`, and `--comment` options.
`create` also accepts repeatable tags and links. `tag` is additive.

Storage directory precedence is `--dir` / `-d`, then `TICKET_BASE_DIR`, then
`base-directory` in `.ticketconfig.yaml` in the current working directory,
then `tickets`. Explicit directories must already exist. The default `tickets`
directory is created on the first write. Configuration is always read from
the current directory, including when `--dir` selects a different store.

## Ticket IDs

Generated IDs have a type prefix and 16 random lowercase letters or digits,
for example `bug-k7m2p9x4c6v8n3qa`. Each prefix has `36^16` possible suffixes.
Existing tickets keep their IDs, and `--overwrite-id` still sets an exact
custom ID.

Text output shows the first four characters after the type prefix: for example,
`bug-k7m2p9x4c6v8n3qa` appears as `k7m2`. More characters are shown when needed
to identify the ticket uniquely across the entire store, even when a list is
filtered or limited. For example, `bug-abcd1...` and `feat-abcd2...` appear as
`abcd1` and `abcd2`. Older IDs with fewer than four suffix characters use the
available characters. If two types have identical full suffixes, their full IDs
are shown so each remains usable.

You can enter these short IDs, or any unambiguous leading part of a full ID:

```sh
ticket show k7m2
ticket modify k7m2 status=fixed
ticket tag k7m2 urgent
ticket comment k7m2 "Verified the fix"
ticket modify k7m2 --link p2qr:related
ticket modify k7m2 --unlink p2qr:related
ticket ls id=k7m2
ticket search crash --filter id=k7m2
```

Both full-ID prefixes and suffix prefixes are case-sensitive. An exact ID takes
precedence over longer IDs starting with the same characters. If a prefix
matches several tickets, the command lists the matches and fails without
saving changes; enter more characters to select one. Commands report an error
when no ticket matches (unlinking an unknown target remains a no-op).
`--overwrite-id` always uses the supplied value literally.

JSON and YAML output always retain full IDs, including relationship targets,
back-references, and overview results. Ticket files and stored relationships
also retain full IDs. Abbreviations are resolved when entered at the command
line; stored relationship IDs must be exact. Dangling relationship targets can
also be abbreviated when unlinking.

## Storage and configuration

Run `ticket config` or `ticket config --help` for the configuration options,
defaults, format rules, and a complete YAML example. This help also works when
the current configuration is invalid. Use `ticket overview` to inspect the
active configuration.

The current format is **v0.5**. Put this at the top of `.ticketconfig.yaml`:

```yaml
version: v0.5
```

Versions follow [Semantic Versioning](https://semver.org/), with an optional
leading `v` and an optional patch number: `v0.5` means `0.5.0`. Values must be
strings; quote versions if YAML would otherwise read them as numbers, such as
`version: "0.5"`. Major version changes are breaking changes, and within major
zero, minor version changes are also breaking changes. The app accepts stable
`0.5.x` formats, including patch versions with build metadata. Prerelease
formats require an exact release match.

Incompatible versions fail before any store reads or writes, with an error
when no import/migration is available. No migrations are currently provided.
An existing config with no version, or an existing ticket store with no config,
is treated as unversioned and rejected. Changing the version label alone does
not migrate an older store.

In a fresh project with no config or tickets, the first successful `create`
records `version: v0.5` in `.ticketconfig.yaml`. Read-only commands in an empty
project do not create files. The overview includes the config format version.

Tickets are human-readable YAML files named `<id>.yaml`. Run `ticket` from your
project directory to use its store and `.ticketconfig.yaml`. Saves use a
temporary file followed by an atomic replacement. Tickets and configuration
can be tracked alongside your project in Git.

See [the example configuration](tests/fixtures/config.yaml) for types, initial
states, transitions, reverse relationship names, and target type restrictions.
Top-level `tags: [urgent, easy]` restricts tags. Missing/null `types` imposes no
type restrictions; `types: "*"` permits all types; a bare type entry permits
all states. A full type definition requires `initial-state` and a nonempty
`states` list. Bare states allow all transitions; `transitions: []` makes a
state terminal. For example:

```yaml
version: v0.5
types:
  - bug:
      initial-state: open
      states:
        - open: {transitions: [fixed, wontfix]}
        - fixed: {transitions: []}
        - wontfix: {transitions: []}
```

Use `ticket ls open` as shorthand for `ticket ls status=open`, or
`ticket ls in-progress` for `ticket ls status=in-progress`. This matches the
exact status, whether or not a workflow is configured. A status with no matching
tickets produces an empty result.

Built-in command names are reserved: use `ticket ls status=show` to filter by
a status named `show`; `ticket ls show` reports an error with that explicit form.
The same rule applies to `search --filter`. Filters can be combined, for example
`ticket ls open type=bug`.

Lists default to 25 rows, ordered by last content update; `--limit 0` shows
everything. Tags and comments
do not change `updated`. Failed edits leave the ticket file unchanged.
`verify` reports all problems and returns 1 if any exist (otherwise 0).

Bare `modify --unlink ID` removes each entire relationship entry containing
that ID. Use
`--unlink ID:RELATIONSHIP` to remove only one target from a multiple-target
entry. IDs reject both slash styles to keep paths safe across platforms;
Windows additionally rejects reserved filenames. Fuzzy matching uses a
normalized indel similarity score over text windows. Increase `--min-score`
to require closer matches.

## Development checks

```sh
python -m unittest discover -s tests -v
```

Tests use isolated temporary directories and require no external application
or ticket store.
