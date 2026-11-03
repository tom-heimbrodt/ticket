#!/usr/bin/env python3
"""A portable command-line ticket tracker with YAML storage."""
import argparse
import copy
import json
import math
import os
from pathlib import Path
import re
import secrets
import string
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

try:
    import yaml
except ImportError:
    sys.exit('PyYAML is required. Install it with: python -m pip install PyYAML')

VERSION = '0.5.0'
CONFIG_VERSION = 'v0.5'
ID_SUFFIX_LENGTH = 16
SCALARS = ('id', 'summary', 'description', 'author', 'assignee', 'approver', 'type', 'status', 'priority', 'created', 'updated')
MODIFIABLE = SCALARS[1:9]
SEARCHABLE = SCALARS[:8] + ('tags', 'relationships')
ROW_FIELDS = ('id', 'type', 'status', 'priority', 'assignee', 'summary')
COMMAND_HELP = (
    ('config', 'Describe .ticketconfig.yaml options and format'),
    ('create', 'Create a ticket'), ('ls', 'List tickets'),
    ('search', 'Fuzzily search tickets'), ('show', 'Show ticket details'),
    ('modify', 'Modify a ticket'), ('tag', 'Add tags'),
    ('comment', 'Append a comment'), ('overview', 'Show store and config'),
    ('verify', 'Check all ticket files'),
)
COMMAND_NAMES = frozenset(name for name, _ in COMMAND_HELP)


class TicketError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise TicketError(message)


@dataclass(frozen=True)
class FormatVersion:
    major: int
    minor: int
    patch: int
    prerelease: str = ''
    build: str = ''

    @classmethod
    def parse(cls, value):
        message = (f"invalid config version {value!r}; expected a SemVer string "
                   "such as 'v0.5' or '0.5.0'")
        require(isinstance(value, str), message)
        # Accept a leading v and MAJOR.MINOR shorthand; otherwise SemVer 2.0.
        number = r'(0|[1-9][0-9]*)'
        label = r'[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*'
        match = re.fullmatch(r'v?' + number + r'\.' + number + r'(?:\.' + number
                             + r')?(?:-(' + label + r'))?(?:\+(' + label + r'))?', value)
        require(match is not None, message)
        major, minor, patch, prerelease, build = match.groups()
        prerelease = prerelease or ''
        require(all(not (part.isdigit() and len(part) > 1 and part[0] == '0')
                    for part in prerelease.split('.')), message)
        return cls(int(major), int(minor), int(patch or 0), prerelease, build or '')

    def compatible_with(self, other):
        if self.major != other.major or (self.major == 0 and self.minor != other.minor):
            return False
        # Draft formats require an exact release match; metadata is ignored.
        if self.prerelease or other.prerelease:
            return (self.minor, self.patch, self.prerelease) == (other.minor, other.patch, other.prerelease)
        return True


def check_config_version(value):
    parsed = FormatVersion.parse(value)
    require(parsed.compatible_with(FormatVersion.parse(CONFIG_VERSION)),
            f"config version '{value}' is incompatible with supported format {CONFIG_VERSION}; "
            f"no import/migration from '{value}' to {CONFIG_VERSION} is available")
    return parsed


# YAML 1.2-like scalars: keep timestamps as strings and words such as "on"
# as strings. Never modify PyYAML's global loader.
class Loader(yaml.SafeLoader):
    yaml_implicit_resolvers = {
        k: [(tag, rx) for tag, rx in entries
            if tag not in ('tag:yaml.org,2002:timestamp', 'tag:yaml.org,2002:bool')]
        for k, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


Loader.add_implicit_resolver('tag:yaml.org,2002:bool', re.compile(r'^(?:true|True|TRUE|false|False|FALSE)$'), list('tTfF'))


def mapping(loader, node):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            require(key not in result, f'duplicate YAML key: {key!r}')
            result[key] = loader.construct_object(value_node, deep=True)
        except TypeError as exc:
            raise TicketError('YAML mapping keys must be scalar values') from exc
    return result


Loader.add_constructor('tag:yaml.org,2002:map', mapping)


def read_yaml(path):
    return yaml.load(path.read_text(encoding='utf-8'), Loader=Loader)


def dump_yaml(value):
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True)


def strings(value, context):
    require(isinstance(value, list) and all(isinstance(x, str) for x in value), f'{context} must be a list of strings')
    return value


def one_entry(value, context):
    require(isinstance(value, dict) and len(value) == 1, f'{context} must be a single-key mapping')
    name, rest = next(iter(value.items()))
    require(isinstance(name, str), f'{context} name must be a string')
    return name, rest


def wildcard_list(value, context):
    if isinstance(value, str):
        return '*' if value == '*' else [value]
    return strings(value, context)


class Config:
    def __init__(self, raw=None):
        raw = {'version': CONFIG_VERSION} if raw is None else raw
        require(isinstance(raw, dict), 'config must be a mapping')
        require('version' in raw,
                f'config version is missing (unversioned format); no import/migration '
                f'from an unversioned format to {CONFIG_VERSION} is available')
        self.version = raw['version']
        self.parsed_version = check_config_version(self.version)
        self.base = raw.get('base-directory')
        require(self.base is None or isinstance(self.base, str), 'base-directory must be a string')
        tags = raw.get('tags')
        self.tags = '*' if tags is None else tags
        if self.tags != '*':
            strings(self.tags, 'tags')
        types = raw.get('types')
        self.active = types is not None
        self.any_types = types == '*'
        self.types = {}
        self.any_transitions = set()
        if types is None or types == '*':
            return
        require(isinstance(types, list), "types must be '*', a list, or absent/null")
        for entry in types:
            name, definition = (entry, None) if isinstance(entry, str) else one_entry(entry, 'type')
            require(name not in self.types, f'duplicate type {name!r}')
            td = dict(name=name, initial_state='new', any_states=True, states=[], relationships='none')
            if definition is not None:
                require(isinstance(definition, dict), f'type {name!r} definition must be a mapping')
                initial = definition.get('initial-state')
                require(isinstance(initial, str), f'type {name!r}: initial-state must be a string')
                states = definition.get('states')
                require(isinstance(states, list) and states, f'type {name!r}: states must be a non-empty list')
                td.update(initial_state=initial, any_states=False)
                seen = set()
                for state in states:
                    sn, rest = (state, None) if isinstance(state, str) else one_entry(state, 'state')
                    require(sn not in seen, f'duplicate state {sn!r} of type {name!r}')
                    seen.add(sn)
                    require(rest is None or isinstance(rest, dict), f'state {sn!r} definition must be a mapping')
                    rest = {} if rest is None else rest
                    require(not (set(rest) - {'transitions'}), f'state {sn!r}: unknown key')
                    if rest.get('transitions', '*') == '*':
                        self.any_transitions.add((name, sn))
                    transitions = wildcard_list(rest.get('transitions', '*'), 'transitions')
                    td['states'].append(dict(name=sn, transitions=['*'] if transitions == '*' else transitions))
                rels = definition.get('relationships')
                if rels == '*':
                    td['relationships'] = '*'
                elif rels is not None:
                    require(isinstance(rels, list), 'relationships must be a list or wildcard')
                    td['relationships'] = []
                    seen = set()
                    for rel in rels:
                        rn, rest = one_entry(rel, 'relationship')
                        require(rn not in seen, f'duplicate relationship {rn!r} of type {name!r}')
                        seen.add(rn)
                        rest = {} if rest is None else rest
                        require(isinstance(rest, dict), 'relationship definition must be a mapping')
                        require(not (set(rest) - {'reverse', 'target-types'}), f'relationship {rn!r}: unknown key')
                        reverse = rest.get('reverse', rn)
                        require(isinstance(reverse, str), 'reverse must be a string')
                        targets = wildcard_list(rest.get('target-types', '*'), 'target-types')
                        td['relationships'].append(dict(name=rn, reverse=reverse, target_types=targets))
            self.types[name] = td
        self.types = dict(sorted(self.types.items()))

    def allowed_types(self):
        return '*' if not self.active or self.any_types else ', '.join(self.types) or 'none'

    def allows_type(self, name):
        return not self.active or self.any_types or name in self.types

    def check_status(self, name, status):
        td = self.types.get(name)
        if td and not td['any_states']:
            names = [s['name'] for s in td['states']]
            require(status in names, f"status '{status}' is not a valid state for type '{name}' (valid: {', '.join(names)})")

    def check_tags(self, tags):
        for tag in tags:
            require(self.tags == '*' or tag in self.tags, f"tag '{tag}' is not allowed (allowed tags: {self.tag_names()})")

    def tag_names(self):
        return '*' if self.tags == '*' else ', '.join(self.tags) or 'none'

    def check_relationship(self, source, rel, target):
        src = self.types.get(source)
        if not src or src['relationships'] == '*':
            return
        forward = src['relationships'] if isinstance(src['relationships'], list) else []
        dst = self.types.get(target, {}).get('relationships', [])
        reverse = dst if isinstance(dst, list) else []
        known, covered = False, []
        for d, matched, wanted in ([(d, d['name'] == rel, target) for d in forward]
                                    + [(d, d['reverse'] == rel, source) for d in reverse]):
            if matched:
                known = True
                allowed = d['target_types']
                if allowed == '*' or wanted in allowed:
                    return
                covered.extend(x for x in allowed if x not in covered)
        require(known, f"relationship '{rel}' is not allowed for type '{source}'")
        raise TicketError(f"relationship '{rel}' does not allow a target of type '{target}' (allowed target types: {', '.join(covered) or 'none'})")

    def reverse_name(self, source, rel):
        rels = self.types.get(source, {}).get('relationships', [])
        if self.active and isinstance(rels, list):
            return next((d['reverse'] for d in rels if d['name'] == rel), rel)
        return rel

    def value(self):
        return dict(version=self.version, active=self.active, any_types=self.any_types, allowed_types=self.allowed_types(),
                    tags=['*'] if self.tags == '*' else self.tags, types=list(self.types.values()))


def validate_id(value):
    require(bool(value) and value not in ('.', '..'), 'id must not be empty, dot, or dot-dot')
    require(not any(c in value for c in '/\\\0'), 'id must not contain path separators or NUL bytes')
    if os.name == 'nt':
        require(not re.search(r'[<>:"|?*\x00-\x1f]', value) and not value.endswith((' ', '.')),
                'id is not a valid Windows filename')
        require(value.split('.')[0].upper() not in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(i) for i in range(1, 10)), *('LPT'+str(i) for i in range(1, 10))}, 'id is a reserved Windows filename')


def integer(value):
    require(bool(re.fullmatch(r'[+-]?[0-9]+', str(value))), f"'{value}' is not an integer")
    number = int(value)
    require(-(2**63) <= number < 2**63, f"'{value}' is not an integer in the signed 64-bit range")
    return number


def parse_ticket(value):
    require(isinstance(value, dict), 'ticket must be a mapping')
    t = {}
    for field in SCALARS:
        require(field in value, f'missing field {field!r}')
        v = value[field]
        if field == 'priority':
            require(type(v) is int and -(2**63) <= v < 2**63, 'priority must be a signed 64-bit integer')
        else:
            require(isinstance(v, str), f'{field} must be a string')
        t[field] = v
    t['tags'] = strings(value.get('tags', []), 'tags')
    rels = value.get('relationships')
    rels = {} if rels is None else rels
    require(isinstance(rels, dict), 'relationships must be a mapping; the legacy list format is no longer supported')
    t['relationships'] = {}
    for rel, targets in rels.items():
        require(isinstance(rel, str), 'relationship name must be a string')
        targets = [targets] if isinstance(targets, str) else strings(targets, 'relationship targets')
        require(bool(targets), f"relationship '{rel}' has no targets; remove it")
        require(len(set(targets)) == len(targets), 'relationship target is listed more than once')
        t['relationships'][rel] = targets
    t['relationships'] = dict(sorted(t['relationships'].items()))
    comments = value.get('comments', [])
    require(isinstance(comments, list), 'comments must be a list')
    t['comments'] = []
    for comment in comments:
        require(isinstance(comment, dict), 'comment must be a mapping')
        require(all(isinstance(comment.get(k), str) for k in ('author', 'created', 'text')), 'comment requires author, created and text strings')
        t['comments'].append({k: comment[k] for k in ('author', 'created', 'text')})
    return t


def ticket_value(t):
    # Keep a stable, readable field order in saved tickets.
    result = {k: copy.deepcopy(t[k]) for k in ('id', 'summary', 'description', 'author', 'assignee', 'approver', 'type', 'status', 'priority', 'created', 'updated', 'tags', 'relationships', 'comments')}
    result['relationships'] = {r: ids[0] if len(ids) == 1 else ids for r, ids in sorted(t['relationships'].items())}
    return result


def id_suffix(identifier):
    """The portion after the type prefix, or the whole ID for unprefixed IDs."""
    _, sep, suffix = identifier.partition('-')
    return suffix if sep and suffix else identifier


def matching_ids(query, candidates):
    if query in candidates:
        return [query]
    return sorted(i for i in candidates
                  if i.startswith(query) or id_suffix(i).startswith(query))


class IdDisplay:
    """Short IDs that resolve uniquely across the whole store, across types."""
    def __init__(self, identifiers):
        self.identifiers = set(identifiers)
        self.cache = {}

    def __call__(self, identifier):
        if identifier not in self.cache:
            suffix = id_suffix(identifier)
            result = identifier
            for length in range(min(4, len(suffix)), len(suffix) + 1):
                short = suffix[:length]
                if matching_ids(short, self.identifiers) == [identifier]:
                    result = short
                    break
            self.cache[identifier] = result
        return self.cache[identifier]


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, identifier):
        validate_id(identifier)
        return self.directory / (identifier + '.yaml')

    def exists(self, identifier):
        return self.path(identifier).is_file()

    def ids(self):
        return sorted(p.name[:-5] for p in self.directory.iterdir() if p.name.endswith('.yaml')) if self.directory.is_dir() else []

    def resolve(self, identifier, extra_ids=(), missing_ok=False):
        """Resolve user input; stored IDs and filenames always remain exact."""
        validate_id(identifier)
        if self.exists(identifier):
            return identifier
        candidates = set(extra_ids)
        candidates.update(i for i in self.ids() if self.exists(i))
        if identifier in candidates:
            return identifier
        matches = matching_ids(identifier, candidates)
        require(len(matches) <= 1,
                f"ticket ID '{identifier}' is ambiguous; matches: {', '.join(matches)}")
        if matches:
            return matches[0]
        require(missing_ok, f"ticket '{identifier}' not found")
        return identifier

    def display_id(self, identifier):
        return IdDisplay([*self.ids(), identifier])(identifier)

    def load(self, identifier):
        path = self.path(identifier)
        require(path.is_file(), f"ticket '{identifier}' not found")
        return parse_ticket(read_yaml(path))

    def list(self):
        tickets = []
        for identifier in self.ids():
            try:
                tickets.append(self.load(identifier))
            except (OSError, ValueError, yaml.YAMLError) as exc:
                print(f"warning: skipping unreadable ticket file '{self.path(identifier)}': {exc}", file=sys.stderr)
        return sorted(tickets, key=lambda t: t['id'])

    def save(self, t):
        dest = self.path(t['id'])
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n', dir=self.directory, suffix='.tmp', delete=False) as handle:
                tmp = handle.name
                handle.write(dump_yaml(ticket_value(t)))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, dest)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def git_user():
    try:
        result = subprocess.run(['git', 'config', 'user.name'], capture_output=True, text=True, encoding='utf-8', errors='replace', check=False)
        return result.stdout.strip() if result.returncode == 0 else ''
    except OSError:
        return ''


def split_pair(value, separator, syntax):
    left, sep, right = value.partition(separator)
    require(bool(sep and left and right), f"expected {syntax} with non-empty parts, got '{value}'")
    return left, right


def links_from(values, store):
    result = {}
    for value in values:
        identifier, rel = split_pair(value, ':', 'ID:RELATIONSHIP')
        identifier = store.resolve(identifier)
        targets = result.setdefault(rel, [])
        if identifier not in targets:
            targets.append(identifier)
    return dict(sorted(result.items()))


def check_links(store, config, source, links, existence=True):
    for rel, targets in links.items():
        for identifier in targets:
            if existence:
                require(store.exists(identifier), f"cannot link to '{identifier}': ticket does not exist")
            if config.active:
                config.check_relationship(source, rel, store.load(identifier)['type'])


def create(store, config, args, config_path=None):
    typ = args.ticket_type
    require(config.allows_type(typ), f"type '{typ}' is not allowed (allowed types: {config.allowed_types()})")
    status = args.status if args.status is not None else config.types.get(typ, {}).get('initial_state', 'new')
    config.check_status(typ, status)
    config.check_tags(args.tag)
    links = links_from(args.link, store)
    check_links(store, config, typ, links)
    identifier = args.overwrite_id
    if identifier is None:
        prefix = ''.join(c for c in typ.lower() if c in string.ascii_lowercase + string.digits)[:6]
        for _ in range(1000):
            identifier = prefix + '-' + ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(ID_SUFFIX_LENGTH))
            if not store.exists(identifier):
                break
        else:
            raise TicketError('could not generate a unique id after 1000 attempts')
    validate_id(identifier)
    require(not store.exists(identifier), f"ticket with id '{identifier}' already exists")
    user, stamp = git_user(), now()
    t = dict(id=identifier, summary=args.summary, description=args.description or '',
             author=user if args.author is None else args.author,
             assignee=user if args.assignee is None else args.assignee,
             approver=args.approver or '', type=typ, status=status, priority=args.priority,
             created=stamp, updated=stamp, tags=list(dict.fromkeys(args.tag)), relationships=links, comments=[])
    if config_path is not None:
        # Record the format before the first write to a newly initialized store.
        with config_path.open('x', encoding='utf-8', newline='\n') as handle:
            handle.write(dump_yaml({'version': CONFIG_VERSION}))
    store.save(t)
    print(f'created {store.display_id(identifier)} ({typ}: {args.summary})')


def modify(store, config, args):
    t = store.load(args.id)
    display = IdDisplay([*store.ids(), t['id'],
                         *(i for ids in t['relationships'].values() for i in ids)])
    original_type, original_status = t['type'], t['status']
    changes, metadata = [], []
    config.check_tags(args.tag)
    for tag in args.untag:
        require(tag in t['tags'], f"cannot untag '{tag}': the ticket does not have this tag")
    for assignment in args.assignments:
        field, value = split_pair(assignment, '=', 'FIELD=VALUE')
        require(field in MODIFIABLE, f"field '{field}' is not modifiable; valid fields: {', '.join(MODIFIABLE)}")
        parsed = integer(value) if field == 'priority' else value
        if t[field] != parsed:
            t[field] = parsed
            changes.append(f'{field}={value}')
    if t['status'] != original_status and t['type'] == original_type:
        config.check_status(t['type'], t['status'])
        td = config.types.get(t['type'])
        if td and not td['any_states']:
            state = next((s for s in td['states'] if s['name'] == original_status), None)
            require(state is None or (t['type'], original_status) in config.any_transitions or t['status'] in state['transitions'],
                    f"cannot transition type '{t['type']}' from '{original_status}' to '{t['status']}'")
    links = links_from(args.link, store)
    check_links(store, config, t['type'], links)
    for rel, targets in links.items():
        existing = t['relationships'].setdefault(rel, [])
        for identifier in targets:
            if identifier not in existing:
                existing.append(identifier)
                changes.append(f'+{display(identifier)}:{rel}')
    for unlink in args.unlink:
        identifier, sep, rel = unlink.partition(':')
        # Include dangling targets so broken relationships can still be removed.
        targets = [i for ids in t['relationships'].values() for i in ids]
        identifier = store.resolve(identifier, extra_ids=targets, missing_ok=True)
        removed = False
        for r, targets in list(t['relationships'].items()):
            if (not sep or rel == r) and identifier in targets:
                # A bare ID removes the entire relationship entry containing
                # that ID; ID:RELATIONSHIP removes only the selected target.
                if sep:
                    targets.remove(identifier)
                if not sep or not targets:
                    del t['relationships'][r]
                removed = True
        if removed:
            changes.append('-' + display(identifier) + (':' + rel if sep else ''))
    if t['type'] != original_type:
        config.check_status(t['type'], t['status'])
        check_links(store, config, t['type'], t['relationships'], existence=False)
    for tag in args.tag:
        if tag not in t['tags']:
            t['tags'].append(tag)
            metadata.append('+tag ' + tag)
    for tag in args.untag:
        t['tags'] = [x for x in t['tags'] if x != tag]
        metadata.append('-tag ' + tag)
    for text in args.comment:
        require(bool(text.strip()), 'comment text must not be empty')
        t['comments'].append(dict(author=git_user(), created=now(), text=text))
        metadata.append('+comment')
    if not changes and not metadata:
        print(f"nothing to modify for '{display(t['id'])}'")
        return
    if changes:
        t['updated'] = now()
    store.save(t)
    print(f"modified '{display(t['id'])}': {', '.join(changes + metadata)}")


def stamp_key(stamp):
    try:
        require(bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})', stamp)), 'invalid timestamp')
        dt = datetime.fromisoformat(stamp.upper().replace('Z', '+00:00'))
        fraction = re.search(r'\.(\d+)', stamp)
        nanos = int((fraction[1][:9] if fraction else '').ljust(9, '0'))
        return (1, dt.astimezone(timezone.utc).replace(microsecond=0), nanos)
    except (ValueError, OverflowError):
        return (0, stamp)


def sort_tickets(tickets, method):
    tickets = sorted(tickets, key=lambda t: t['id'])
    if method in ('id', 'relevance'):
        return tickets
    field = 'created' if method in ('new', 'old') else 'updated'
    return sorted(tickets, key=lambda t: stamp_key(t[field]), reverse=method in ('new', 'updated'))


def filtered(tickets, filters, store):
    pairs = []
    for token in filters:
        if '=' not in token:
            require(token not in COMMAND_NAMES,
                    f"'{token}' is a built-in command name; use 'status={token}' to filter by this status")
            token = 'status=' + token
        field, value = split_pair(token, '=', 'FIELD=VALUE')
        require(field in SCALARS or field == 'tag', f"unknown filter field; valid fields: {', '.join(SCALARS + ('tag',))}")
        if field == 'id':
            value = store.resolve(value)
        pairs.append((field, integer(value) if field == 'priority' else value))
    return [t for t in tickets if all(v in t['tags'] if f == 'tag' else t[f] == v for f, v in pairs)]


def ratio(a, b):
    # Exact normalized indel similarity, computed via a bit-parallel LCS.
    if not a and not b:
        return 1.0
    masks = {}
    for i, char in enumerate(a):
        masks[char] = masks.get(char, 0) | (1 << i)
    row = 0
    for char in b:
        x = row | masks.get(char, 0)
        row = x & ~(x - ((row << 1) | 1))
    return 2 * bin(row).count('1') / (len(a) + len(b))


def partial_ratio(query, text):
    if not query:
        return float(not text)
    if len(query) >= len(text):
        return ratio(query, text)
    if query in text:
        return 1.0
    return max(ratio(query, text[i:i + len(query)]) for i in range(len(text) - len(query) + 1))


def normalize(text):
    return ' '.join(text.lower().split())


def score(query, text):
    query, text = normalize(query), normalize(text)
    if not query or not text:
        return 0.0
    parts = [query] + query.split()
    return sum(partial_ratio(p, text) for p in parts) / len(parts)


def emit(value, output):
    print(json.dumps(value, ensure_ascii=False, indent=2) if output == 'json' else dump_yaml(value), end='\n' if output == 'json' else '')


def table(tickets, output, store):
    rows = [{k: t[k] for k in ROW_FIELDS} for t in tickets]
    if output != 'text':
        emit(rows, output)
    elif not rows:
        print('no tickets')
    else:
        display = IdDisplay([*store.ids(), *(t['id'] for t in tickets)])
        for row in rows:
            row['id'] = display(row['id'])
        cells = [['ID', 'TYPE', 'STATUS', 'PRIO', 'ASSIGNEE', 'SUMMARY']] + [[str(r[k]) for k in ROW_FIELDS] for r in rows]
        widths = [max(len(row[i]) for row in cells) for i in range(6)]
        for row in cells:
            print('  '.join(v.ljust(w) for v, w in zip(row, widths)))


def list_or_search(store, config, args):
    tickets = filtered(store.list(), args.filters, store)
    if args.command == 'ls':
        require(args.limit >= 0, f'expected a non-negative integer for --limit, got {args.limit}')
        tickets = sort_tickets(tickets, args.sort)
        if args.limit:
            tickets = tickets[:args.limit]
    else:
        require(math.isfinite(args.min_score) and 0 <= args.min_score <= 1, '--min-score must be a number between 0 and 1')
        fields = list(dict.fromkeys(f.strip() for f in args.fields.split(',') if f.strip()))
        require(bool(fields), 'no fields given in --fields')
        require(all(f in SEARCHABLE for f in fields), f"unknown search field; valid fields: {', '.join(SEARCHABLE)}")
        scored = []
        for t in tickets:
            text = ' '.join(' '.join(t[f]) if f in ('tags', 'relationships') else t[f] for f in fields)
            s = score(' '.join(args.search), text)
            if s >= args.min_score:
                scored.append((s, t))
        tickets = [t for s, t in sorted(scored, key=lambda pair: (-pair[0], pair[1]['id']))]
        if args.sort != 'relevance':
            tickets = sort_tickets(tickets, args.sort)
    table(tickets, args.output, store)


def references(store, config, identifier):
    return [dict(id=t['id'], relationship=r, reverse_name=config.reverse_name(t['type'], r))
            for t in store.list() for r, targets in t['relationships'].items() if identifier in targets]


def show(store, config, args):
    t = store.load(args.id)
    refs = references(store, config, t['id'])
    if args.output != 'text':
        value = ticket_value(t)
        value['referenced_by'] = refs
        emit(value, args.output)
        return
    display = IdDisplay([*store.ids(), t['id'], *(ref['id'] for ref in refs),
                         *(i for ids in t['relationships'].values() for i in ids)])
    for label, field in [('ID', 'id'), ('Type', 'type'), ('Status', 'status'), ('Priority', 'priority'), ('Summary', 'summary'), ('Author', 'author'), ('Assignee', 'assignee'), ('Approver', 'approver'), ('Created', 'created'), ('Updated', 'updated')]:
        value = display(t[field]) if field == 'id' else t[field]
        print(f'{label + ":":12}{value}')
    print('\nDescription:')
    print(t['description'], end='\n' if t['description'] and not t['description'].endswith('\n') else '')
    print('Comments:')
    if not t['comments']:
        print('  (none)')
    for comment in t['comments']:
        print(f"  {comment['created']} {comment['author']}:")
        for line in comment['text'].splitlines():
            print('    ' + line)
    print('Relationships:')
    if not t['relationships']:
        print('  (none)')
    for rel, targets in t['relationships'].items():
        for identifier in targets:
            print(f'  -> {display(identifier)} ({rel})')
    print('Referenced by:')
    if not refs:
        print('  (none)')
    for ref in refs:
        print(f"  <- {display(ref['id'])} ({ref['reverse_name']})")


def overview(store, config, output):
    tickets = store.list()
    counts = {key: dict(sorted(Counter(t[key] for t in tickets).items())) for key in ('type', 'status')}
    recent = sort_tickets(tickets, 'updated')[:1]
    data = dict(dir=str(store.directory), total=len(tickets), by_type=counts['type'], by_status=counts['status'])
    if recent:
        data['most_recently_updated'] = {k: recent[0][k] for k in ('id', 'updated', 'summary')}
    if output != 'text':
        emit(dict(store=data, config=config.value()), output)
        return
    print(f'Store overview ({len(tickets)} ticket(s) in {store.directory})')
    if not tickets:
        print('(empty store)')
    else:
        for key in ('type', 'status'):
            print(f'\nBy {key}:')
            for name, count in counts[key].items():
                print(f'  {name:12} {count}')
        t = recent[0]
        print(f"\nMost recently updated:\n  {store.display_id(t['id'])}  {t['updated']}  {t['summary']}")
    print('\nConfig schema:')
    print(f'  version: {config.version}')
    if not config.active:
        print('  inactive - no type/tag constraints are enforced')
        return
    print(f'  types: {config.allowed_types()}\n  tags: {config.tag_names()}')
    for td in config.types.values():
        print(f"  type {td['name']}:\n    initial state: {td['initial_state']}")
        if td['any_states']:
            print('    states: * (any state, any transition)')
        for state in td['states']:
            print(f"    state {state['name']}: -> {', '.join(state['transitions']) or '(none)'}")
        rels = td['relationships']
        if isinstance(rels, str):
            print('    relationships: ' + ('none allowed' if rels == 'none' else '* (any)'))
        else:
            for rel in rels:
                targets = rel['target_types']
                targets = targets if isinstance(targets, str) else ', '.join(targets)
                reverse = ', reverse: ' + rel['reverse'] if rel['reverse'] != rel['name'] else ''
                print(f"    relationship {rel['name']}: targets {targets}{reverse}")


def verify(store, config, output):
    issues = []
    for identifier in store.ids():
        prefix = identifier + '.yaml: '
        try:
            raw = read_yaml(store.path(identifier))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            issues.append(prefix + 'unreadable or invalid YAML: ' + str(exc))
            continue
        try:
            t = parse_ticket(raw)
        except TicketError as exc:
            issues.append(prefix + 'not a valid ticket: ' + str(exc))
            continue
        if t['id'] != identifier:
            issues.append(prefix + f"ticket id '{t['id']}' does not match its file name")
        for name in ('created', 'updated'):
            if stamp_key(t[name])[0] == 0:
                issues.append(prefix + f"{name} '{t[name]}' is not a valid RFC3339 timestamp")
        for targets in t['relationships'].values():
            for target in targets:
                try:
                    require(store.exists(target), f"relationship target '{target}' does not exist")
                except TicketError as exc:
                    issues.append(prefix + str(exc))
        if not config.allows_type(t['type']):
            issues.append(prefix + f"type '{t['type']}' is not allowed by the config (allowed types: {config.allowed_types()})")
        try:
            config.check_status(t['type'], t['status'])
        except TicketError as exc:
            issues.append(prefix + str(exc))
        for tag in t['tags']:
            try:
                config.check_tags([tag])
            except TicketError as exc:
                issues.append(prefix + str(exc))
        for comment in t['comments']:
            if stamp_key(comment['created'])[0] == 0:
                issues.append(prefix + f"comment created '{comment['created']}' is not a valid RFC3339 timestamp")
        if config.active:
            for rel, targets in t['relationships'].items():
                for target in targets:
                    try:
                        target_ticket = store.load(target)
                    except (OSError, ValueError, yaml.YAMLError):
                        continue
                    try:
                        config.check_relationship(t['type'], rel, target_ticket['type'])
                    except TicketError as exc:
                        issues.append(prefix + str(exc))
    if output != 'text':
        emit(issues, output)
    else:
        for issue in issues:
            print('error: ' + issue, file=sys.stderr)
    return int(bool(issues))


CONFIG_HELP = """Configuration format: .ticketconfig.yaml

The config is a YAML mapping read from the current working directory.
Selecting a different ticket directory does not change the config location.
This command prints documentation; use 'ticket overview' to inspect the
active configuration.

Top-level options:
  version         Required format version string, currently v0.5 (0.5.0).
                  An optional leading 'v' and omitted patch are accepted.
                  Quote numeric-looking values, e.g. version: "0.5".
                  Major changes and 0.x minor changes require migration.
                  Stable 0.5.x patches are compatible; build metadata is
                  ignored. Prereleases require an exact release match.
                  Unsupported or unversioned formats fail: no migrations
                  are currently available. Relabeling is not a migration.
  base-directory  Ticket directory. Default: tickets. Relative paths are
                  relative to the working directory. Precedence:
                  --dir/-d > TICKET_BASE_DIR > base-directory > tickets.
                  Explicitly selected directories must already exist.
  tags            Allowed tags: a list of strings, "*", or absent/null.
                  Absent/null or "*" allows any tag; [] allows none.
  types           Allowed ticket types: a list, "*", or absent/null.
                  Absent/null or "*" allows all types and statuses;
                  [] allows no types on create. A bare entry such as
                  '- bug' (or '- bug: null') allows any state for that
                  type, with initial status new.

Full type definitions (each list item maps one type name to its options):
  initial-state   Required string; default status when creating a ticket.
                  It must be one of the type's states when used.
  states          Required nonempty list of state names or state mappings.
                  A bare state, null definition, or omitted transitions
                  allows transitions to any state of the type.
    transitions   "*", one state name, or a list of state names.
                  [] makes a state terminal (no outgoing transitions).
                  State names must be strings.
  relationships   "*" allows any relationship and target type. Otherwise,
                  a list of single-key mappings declares relationships;
                  absent/null declares none. Each declaration accepts:
    reverse       Optional reverse display/name, defaults to the forward
                  name. Used in the target's 'Referenced by' section.
    target-types  "*" (default), one type name, or a list of type names.
                  [] permits no target types for that declaration.
                  A link is allowed if either the source's forward
                  declaration or the target's reverse declaration permits
                  it. Unconfigured source types are unconstrained.

Duplicate type, state, and relationship names are rejected. Quote "*" in
YAML so it is read as a string, rather than YAML alias syntax.

Example:
  version: v0.5
  # base-directory: tickets  # Create this directory before selecting it.
  tags: [urgent, easy]
  types:
    - bug:
        initial-state: open
        states:
          - open: {transitions: [fixed, wontfix]}
          - fixed: {transitions: []}
          - wontfix: {transitions: []}
        relationships:
          - blocks:
              reverse: blocked-by
              target-types: [bug]
    - note

A fresh project's first successful 'ticket create' writes version: v0.5
if no config exists. Existing stores without a config, or configs without
a version, are rejected as unversioned. Help commands do not read or write
the config or ticket store, so they work even with a broken configuration.
"""


def parser():
    p = argparse.ArgumentParser(prog='ticket', description='A small CLI ticket system storing one YAML file per ticket. With no command, print the overview.',
                                epilog="Run 'ticket config' for .ticketconfig.yaml options, format, and examples.")
    p.add_argument('-V', '--version', action='version', version='ticket ' + VERSION)
    p.add_argument('-d', '--dir', help='Directory containing ticket files')
    subs = p.add_subparsers(dest='command')
    for command, help_text in COMMAND_HELP:
        if command == 'config':
            subs.add_parser(command, help=help_text, description=CONFIG_HELP,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
            continue
        sub = subs.add_parser(command, help=help_text)
        sub.add_argument('-d', '--dir', default=argparse.SUPPRESS, help='Directory containing ticket files')
        if command in ('ls', 'search', 'show', 'overview', 'verify'):
            sub.add_argument('--output', choices=('text', 'json', 'yaml'), default='text')
        if command in ('create', 'comment'):
            sub.add_argument('--author')
        if command == 'create':
            for name in ('assignee', 'approver', 'status', 'overwrite-id'):
                sub.add_argument('--' + name)
            sub.add_argument('--priority', type=integer, default=0)
            sub.add_argument('ticket_type')
            sub.add_argument('summary')
            sub.add_argument('description', nargs='?')
        if command in ('create', 'modify'):
            for name in ('link', 'tag'):
                sub.add_argument('--' + name, action='append', default=[],
                                 help='Target ID or unique prefix:RELATIONSHIP' if name == 'link' else None)
        if command in ('show', 'modify', 'tag', 'comment'):
            sub.add_argument('id', help='Full ID, unique full-ID prefix, or unique suffix prefix')
        if command == 'modify':
            sub.add_argument('assignments', nargs='*')
            for name in ('unlink', 'untag', 'comment'):
                sub.add_argument('--' + name, action='append', default=[],
                                 help='Target ID or unique prefix, optionally :RELATIONSHIP' if name == 'unlink' else None)
        if command == 'tag':
            sub.add_argument('tags', nargs='+')
        if command == 'comment':
            sub.add_argument('text', nargs='+')
        if command == 'ls':
            sub.add_argument('filters', nargs='*',
                             help='AND-combined filters, e.g. open type=bug; bare STATE means status=STATE unless STATE is a built-in command name')
            sub.add_argument('--limit', type=integer, default=25)
            sub.add_argument('--sort', choices=('id', 'new', 'updated', 'old', 'stale'), default='updated')
        if command == 'search':
            sub.add_argument('search', nargs='+')
            sub.add_argument('--fields', default='summary,description')
            sub.add_argument('--filter', dest='filters', action='append', default=[],
                             help='FIELD=VALUE or a bare status; command names require status=NAME')
            sub.add_argument('--sort', choices=('relevance', 'new', 'updated', 'old', 'stale'), default='relevance')
            sub.add_argument('--min-score', type=float, default=0.6)
    return p


def run(argv=None):
    p = parser()
    # argparse's star positionals stop at options; parse subcommands with
    # intermixed arguments so FIELD=VALUE can also follow repeatable options.
    argv = list(sys.argv[1:] if argv is None else argv)
    subcommands = next(action for action in p._actions if isinstance(action, argparse._SubParsersAction)).choices
    command_index = None
    skip = False
    for i, token in enumerate(argv):
        if skip:
            skip = False
            continue
        if token in ('-d', '--dir'):
            skip = True
        elif token in subcommands:
            command_index = i
            break
    if command_index is None:
        args = p.parse_args(argv)
    else:
        before = p.parse_args(argv[:command_index])
        sub = subcommands[argv[command_index]]
        args = sub.parse_intermixed_args(argv[command_index + 1:], namespace=before)
        args.command = argv[command_index]
    if args.command == 'config':
        print(subcommands['config'].format_help(), end='')
        return 0
    path = Path('.ticketconfig.yaml')
    config_exists = path.exists()
    raw_config = read_yaml(path) if config_exists else None
    config = Config({} if config_exists and raw_config is None else raw_config)
    env_dir = os.environ.get('TICKET_BASE_DIR', '').strip()
    config_dir = (config.base or '').strip()
    directory = args.dir if args.dir is not None else env_dir or config_dir or 'tickets'
    source = '--dir' if args.dir is not None else '$TICKET_BASE_DIR' if env_dir else '.ticketconfig.yaml' if config_dir else None
    store = Store(directory)
    if source:
        require(store.directory.is_dir(), f"ticket directory '{directory}' (from {source}) does not exist or is not a directory")
    require(config_exists or not store.ids(),
            f'ticket store is unversioned: .ticketconfig.yaml is missing; no import/migration '
            f'from an unversioned format to {CONFIG_VERSION} is available')
    command = args.command or 'overview'
    if command in ('show', 'modify', 'tag', 'comment'):
        args.id = store.resolve(args.id)
    if command == 'create':
        create(store, config, args, config_path=None if config_exists else path)
    elif command == 'modify':
        modify(store, config, args)
    elif command in ('ls', 'search'):
        list_or_search(store, config, args)
    elif command == 'show':
        show(store, config, args)
    elif command == 'overview':
        overview(store, config, getattr(args, 'output', 'text'))
    elif command == 'verify':
        return verify(store, config, args.output)
    elif command == 'tag':
        t = store.load(args.id)
        config.check_tags(args.tags)
        added = list(dict.fromkeys(x for x in args.tags if x not in t['tags']))
        t['tags'].extend(added)
        store.save(t)
        print(f"tagged '{store.display_id(t['id'])}': {', '.join(added) or '(no new tags)'}")
    elif command == 'comment':
        t = store.load(args.id)
        text = ' '.join(args.text)
        require(bool(text.strip()), 'comment text must not be empty')
        author = git_user() if args.author is None else args.author
        t['comments'].append(dict(author=author, created=now(), text=text))
        store.save(t)
        print(f"commented on '{store.display_id(t['id'])}': {author}: {text.splitlines()[0]}")
    return 0


def main():
    try:
        return run()
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
