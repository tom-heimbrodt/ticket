import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ticket

SCRIPT = str(Path(ticket.__file__).resolve())
FIXTURES = Path(__file__).parent / 'fixtures'
CONFIG = '''version: v0.5
tags: [urgent, easy]
types:
  - bug:
      initial-state: open
      states:
        - open: {transitions: [fixed]}
        - fixed: {transitions: []}
      relationships:
        - blocks: {reverse: blocked-by, target-types: bug}
  - feat:
      initial-state: new
      states: [new, implemented]
'''


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)
        self.env = dict(os.environ)
        self.env.pop('TICKET_BASE_DIR', None)
        self.env['GIT_CONFIG_GLOBAL'] = os.devnull
        self.env['GIT_CONFIG_NOSYSTEM'] = '1'

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, SCRIPT, *args], cwd=self.cwd, env=self.env, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, code, result.stderr)
        return result.stdout

    def create(self, identifier='bug-a', typ='bug', *args):
        return self.cli('create', typ, 'Café startup crash', '--overwrite-id', identifier, *args)

    def load(self, identifier='bug-a'):
        return ticket.Store(self.cwd / 'tickets').load(identifier)

    def configure(self):
        (self.cwd / '.ticketconfig.yaml').write_text(CONFIG, encoding='utf-8')

    def test_config_command_prints_the_same_documentation_as_help(self):
        documentation = self.cli('config')
        self.assertEqual(documentation, self.cli('config', '--help'))
        self.assertEqual(documentation, self.cli('config', '-h'))
        self.assertIn('.ticketconfig.yaml', documentation)
        self.assertIn('version: v0.5', documentation)
        self.assertIn("'ticket config'", self.cli('--help'))
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_config_help_is_available_with_broken_config_and_missing_store(self):
        path = self.cwd / '.ticketconfig.yaml'
        for content in ('broken: [', 'version: v0.4\n', 'types: "*"\n'):
            with self.subTest(content=content):
                path.write_text(content)
                self.env['TICKET_BASE_DIR'] = 'missing-store'
                documentation = self.cli('config')
                self.assertEqual(documentation, self.cli('config', '--help'))
                self.assertEqual(documentation, self.cli('--dir', 'also-missing', 'config'))
                self.assertEqual(path.read_text(), content)
                self.assertEqual(list(self.cwd.iterdir()), [path])

    def test_full_lifecycle_and_machine_shapes(self):
        self.create()
        self.assertEqual(self.load()['status'], 'new')
        self.cli('modify', 'bug-a', 'status=active', 'description=a=b', 'priority=+02')
        self.assertEqual(self.load()['priority'], 2)
        self.assertEqual(self.load()['description'], 'a=b')
        self.cli('tag', 'bug-a', 'urgent', 'urgent')
        self.cli('comment', 'bug-a', '--author', 'Alice', 'hello', 'world')
        shown = json.loads(self.cli('show', 'bug-a', '--output', 'json'))
        self.assertEqual(shown['tags'], ['urgent'])
        self.assertEqual(shown['comments'][0]['text'], 'hello world')
        self.assertEqual(shown['referenced_by'], [])
        rows = json.loads(self.cli('ls', 'tag=urgent', '--output', 'json'))
        self.assertEqual(set(rows[0]), set(ticket.ROW_FIELDS))
        self.assertEqual(self.cli('verify', '--output', 'json').strip(), '[]')
        self.assertEqual(json.loads(self.cli('overview', '--output', 'json'))['store']['total'], 1)
        self.assertIn('Store overview (1 ticket(s)', self.cli())

    def test_generated_ids_have_sixteen_random_characters(self):
        self.cli('create', 'Bug-Fix', 'First ticket')
        self.cli('create', 'Bug-Fix', 'Second ticket')
        ids = ticket.Store(self.cwd / 'tickets').ids()
        self.assertEqual(len(ids), 2)
        for identifier in ids:
            self.assertRegex(identifier, r'^bugfix-[a-z0-9]{16}$')
            self.assertEqual(json.loads(self.cli('show', identifier, '--output', 'json'))['id'], identifier)

    def test_fresh_store_records_format_version(self):
        config_path = self.cwd / '.ticketconfig.yaml'
        self.cli('overview')
        self.assertFalse(config_path.exists())
        self.cli('create', 'bug', 'Invalid', '--overwrite-id', '../escape', code=1)
        self.assertFalse(config_path.exists())
        self.create()
        self.assertEqual(ticket.read_yaml(config_path), {'version': 'v0.5'})
        self.assertEqual(json.loads(self.cli('overview', '--output', 'json'))['config']['version'], 'v0.5')
        self.assertIn('  version: v0.5', self.cli('overview'))

    def test_config_version_is_checked_before_reads_and_writes(self):
        self.create()
        config_path = self.cwd / '.ticketconfig.yaml'
        store = ticket.Store(self.cwd / 'tickets')
        before = store.path('bug-a').read_bytes()
        versions = ['v0.4', 'v0.6', 'v1.0', 'v2.0.0', 'v0.5.0-rc.1']
        commands = [[], ['ls'], ['verify'], ['show', 'bug-a'],
                    ['modify', 'bug-a', 'priority=9'], ['create', 'bug', 'New'],
                    ['comment', 'bug-a', 'Note'], ['tag', 'bug-a', 'urgent']]
        for version in versions:
            config_path.write_text(ticket.dump_yaml({'version': version}))
            original_config = config_path.read_bytes()
            for command in commands:
                with self.subTest(version=version, command=command):
                    result = subprocess.run([sys.executable, SCRIPT, *command], cwd=self.cwd, env=self.env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn('no import/migration', result.stderr)
                    self.assertIn(version, result.stderr)
                    self.assertEqual(store.ids(), ['bug-a'])
                    self.assertEqual(store.path('bug-a').read_bytes(), before)
                    self.assertEqual(config_path.read_bytes(), original_config)
        # Help and the application version remain accessible for troubleshooting.
        self.cli('--help')
        self.cli('--version')

    def test_missing_or_invalid_config_versions_are_not_silently_assumed(self):
        self.create()
        config_path = self.cwd / '.ticketconfig.yaml'
        for content in ('', 'types: "*"\n', 'version: null\n', 'version: 0.5\n', 'version: v0.05\n'):
            with self.subTest(content=content):
                config_path.write_text(content)
                self.cli('ls', code=1)
                self.cli('create', 'bug', 'New', code=1)
                self.assertEqual(config_path.read_text(), content)
        config_path.unlink()
        self.cli('ls', code=1)
        self.cli('create', 'bug', 'New', code=1)
        self.assertFalse(config_path.exists())

    def test_compatible_config_version_is_preserved(self):
        self.create()
        config_path = self.cwd / '.ticketconfig.yaml'
        for version in ('0.5.0', 'v0.5.3', 'v0.5.99+build.007'):
            content = ticket.dump_yaml({'version': version, 'tags': ['urgent']})
            config_path.write_text(content)
            self.cli('modify', 'bug-a', '--tag', 'urgent')
            overview = ticket.yaml.load(self.cli('overview', '--output', 'yaml'), Loader=ticket.Loader)
            self.assertEqual(overview['config']['version'], version)
            self.assertEqual(config_path.read_text(), content)

    def test_unique_prefixes_work_in_all_ticket_commands(self):
        identifier = 'bug-abc1234567890123'
        self.create(identifier)
        self.assertEqual(json.loads(self.cli('show', 'bug-ab', '--output', 'json'))['id'], identifier)
        self.cli('modify', 'bug-ab', 'priority=5')
        self.cli('tag', 'bug-ab', 'urgent')
        self.cli('comment', 'bug-ab', '--author', 'Alice', 'Note')
        t = self.load(identifier)
        self.assertEqual(t['priority'], 5)
        self.assertEqual(t['tags'], ['urgent'])
        self.assertEqual(t['comments'][0]['text'], 'Note')
        for command in (['ls', 'id=bug-ab'], ['search', 'startup', '--filter', 'id=bug-ab']):
            rows = json.loads(self.cli(*command, '--output', 'json'))
            self.assertEqual([t['id'] for t in rows], [identifier])
        self.assertEqual(ticket.Store(self.cwd / 'tickets').ids(), [identifier])

    def test_text_ids_use_four_characters_and_expand_across_the_store(self):
        first = 'bug-abcd111111111111'
        second = 'feat-abcd222222222222'
        third = 'bug-wxyz333333333333'
        self.create(first)
        self.create(second, 'feat')
        self.create(third)
        rows = self.cli('ls', '--sort', 'id', '--limit', '0').splitlines()[1:]
        self.assertEqual([r.split()[0] for r in rows], ['abcd1', 'wxyz', 'abcd2'])
        # A filter or limit must not hide a collision elsewhere in the store.
        self.assertEqual(self.cli('ls', 'id=' + first).splitlines()[1].split()[0], 'abcd1')
        self.assertEqual(self.cli('ls', '--sort', 'id', '--limit', '1').splitlines()[1].split()[0], 'abcd1')
        self.assertEqual(self.cli('search', 'startup', '--filter', 'id=' + first).splitlines()[1].split()[0], 'abcd1')
        for identifier, short in ((first, 'abcd1'), (second, 'abcd2'), (third, 'wxyz')):
            self.assertEqual(json.loads(self.cli('show', short, '--output', 'json'))['id'], identifier)
        self.cli('show', 'abcd', code=1)

    def test_short_ids_work_in_mutations_and_confirmations(self):
        source, target = 'bug-k7m2p9x4c6v8n3qa', 'feat-p2qr123456789012'
        self.assertIn('created k7m2 ', self.create(source))
        self.create(target, 'feat')
        self.assertIn("modified 'k7m2': +p2qr:related", self.cli('modify', 'k7m2', '--link', 'p2qr:related'))
        self.assertIn("tagged 'k7m2': urgent", self.cli('tag', 'k7m2', 'urgent'))
        self.assertIn("commented on 'k7m2': Alice: note", self.cli('comment', 'k7m2', '--author', 'Alice', 'note'))
        self.assertIn("nothing to modify for 'k7m2'", self.cli('modify', 'k7m2'))
        self.assertIn("modified 'k7m2': -p2qr:related", self.cli('modify', 'k7m2', '--unlink', 'p2qr:related'))
        self.cli('modify', 'k7m2', '--link', 'p2qr:related')
        self.assertEqual(self.load(source)['relationships'], {'related': [target]})
        self.assertEqual(ticket.Store(self.cwd / 'tickets').ids(), [source, target])

    def test_show_and_overview_abbreviate_only_text(self):
        source, target = 'bug-k7m2p9x4c6v8n3qa', 'feat-p2qr123456789012'
        self.create(source)
        self.create(target, 'feat')
        self.cli('modify', source, '--link', target + ':related')
        detail = self.cli('show', source, '--output', 'text')
        self.assertIn('ID:         k7m2\n', detail)
        self.assertIn('  -> p2qr (related)', detail)
        self.assertNotIn(source, detail)
        self.assertNotIn(target, detail)
        self.assertIn('  <- k7m2 (related)', self.cli('show', target))
        overview = self.cli('overview')
        self.assertNotIn(source, overview)
        self.assertNotIn(target, overview)
        for fmt in ('json', 'yaml'):
            def decode(*command):
                return ticket.yaml.load(self.cli(*command, '--output', fmt), Loader=ticket.Loader)
            with self.subTest(fmt=fmt):
                rows = decode('ls', '--limit', '0')
                self.assertEqual({r['id'] for r in rows}, {source, target})
                self.assertEqual({r['id'] for r in decode('search', 'startup')}, {source, target})
                detail = decode('show', 'k7m2')
                self.assertEqual(detail['id'], source)
                self.assertEqual(detail['relationships'], {'related': target})
                self.assertEqual(decode('show', 'p2qr')['referenced_by'][0]['id'], source)
                self.assertIn(decode('overview')['store']['most_recently_updated']['id'], (source, target))

    def test_identical_suffixes_keep_full_ids_to_remain_unambiguous(self):
        source, target = 'bug-abcdefghijklmnop', 'feat-abcdefghijklmnop'
        self.create(source)
        self.create(target, 'feat')
        ids = [r.split()[0] for r in self.cli('ls', '--sort', 'id').splitlines()[1:]]
        self.assertEqual(ids, [source, target])
        for identifier in ids:
            self.assertEqual(json.loads(self.cli('show', identifier, '--output', 'json'))['id'], identifier)
        self.cli('show', 'abcdefghijklmnop', code=1)

    def test_relationship_prefixes_are_saved_as_full_ids(self):
        self.configure()
        source, target = 'bug-aaaaaaaaaaaaaaaa', 'bug-bbbbbbbbbbbbbbbb'
        self.create(source)
        self.create(target)
        self.cli('modify', 'bug-aa', '--link', 'bug-bb:blocks', '--link', target + ':blocks')
        self.assertEqual(self.load(source)['relationships'], {'blocks': [target]})
        ref = json.loads(self.cli('show', 'bug-bb', '--output', 'json'))['referenced_by'][0]
        self.assertEqual(ref['id'], source)
        self.cli('modify', 'bug-aa', '--unlink', 'bug-bb:blocks')
        self.assertEqual(self.load(source)['relationships'], {})
        self.create('bug-cccccccccccccccc', 'bug', '--link', 'bug-bb:blocks')
        self.assertEqual(self.load('bug-cccccccccccccccc')['relationships'], {'blocks': [target]})
        self.cli('modify', 'bug-cc', '--unlink', 'bug-bb')
        self.assertEqual(self.load('bug-cccccccccccccccc')['relationships'], {})

    def test_ambiguous_prefixes_report_matches_without_writing(self):
        self.create('bug-abc1111111111111')
        self.create('bug-abc2222222222222')
        self.create('feat-a', 'feat')
        store = ticket.Store(self.cwd / 'tickets')
        before = {i: store.path(i).read_bytes() for i in store.ids()}
        commands = [
            ['show', 'bug-abc'], ['modify', 'bug-abc', 'priority=9'],
            ['tag', 'bug-abc', 'urgent'], ['comment', 'bug-abc', 'note'],
            ['modify', 'feat-a', 'priority=9', '--link', 'bug-abc:blocks'],
            ['modify', 'feat-a', 'priority=9', '--unlink', 'bug-abc'],
            ['create', 'feat', 'new', '--overwrite-id', 'feat-b', '--link', 'bug-abc:blocks'],
            ['ls', 'id=bug-abc'], ['search', 'startup', '--filter', 'id=bug-abc'],
        ]
        for command in commands:
            with self.subTest(command=command):
                result = subprocess.run([sys.executable, SCRIPT, *command], cwd=self.cwd, env=self.env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertIn('ambiguous', result.stderr)
                self.assertIn('bug-abc1111111111111', result.stderr)
                self.assertIn('bug-abc2222222222222', result.stderr)
                self.assertEqual({i: store.path(i).read_bytes() for i in store.ids()}, before)

    def test_exact_ids_take_precedence_and_custom_ids_stay_literal(self):
        self.create('bug-a')
        self.create('bug-abcdef0123456789')
        self.assertEqual(json.loads(self.cli('show', 'bug-a', '--output', 'json'))['id'], 'bug-a')
        self.create('bug-ab')
        self.assertEqual(self.load('bug-ab')['id'], 'bug-ab')
        self.cli('show', 'missing', code=1)
        self.cli('modify', 'bug-ab', '--link', 'missing:blocks', code=1)

    def test_dangling_targets_can_be_unlinked_but_are_not_resolved_on_read(self):
        self.create('bug-aaaaaaaaaaaaaaaa')
        store = ticket.Store(self.cwd / 'tickets')
        t = store.load('bug-aaaaaaaaaaaaaaaa')
        t['relationships'] = {'blocks': ['bug-aaaa', 'missing-long-id']}
        store.save(t)
        issues = json.loads(self.cli('verify', '--output', 'json', code=1))
        self.assertEqual(len(issues), 2)
        self.cli('modify', 'bug-aa', '--unlink', 'missing-l:blocks')
        self.assertEqual(self.load(t['id'])['relationships'], {'blocks': ['bug-aaaa']})
        self.cli('modify', 'bug-aa', '--unlink', 'bug-aaaa:blocks')
        self.assertEqual(self.load(t['id'])['relationships'], {})

    def test_generated_id_retries_local_collisions(self):
        self.create('bug-' + 'a' * 16)
        store = ticket.Store(self.cwd / 'tickets')
        args = ticket.parser().parse_args(['create', 'bug', 'Retry'])
        with patch('ticket.secrets.choice', side_effect=list('a' * 16 + 'b' * 16)) as choose, patch('builtins.print'):
            ticket.create(store, ticket.Config(), args)
        self.assertEqual(choose.call_count, 32)
        self.assertTrue(store.exists('bug-' + 'b' * 16))

    def test_workflow_and_transaction_failure(self):
        self.configure()
        self.create()
        self.assertEqual(self.load()['status'], 'open')
        self.cli('modify', 'bug-a', 'status=fixed')
        path = self.cwd / 'tickets/bug-a.yaml'
        before = path.read_bytes()
        self.cli('modify', 'bug-a', 'summary=changed', 'status=open', code=1)
        self.assertEqual(path.read_bytes(), before)
        self.cli('modify', 'bug-a', 'summary=changed', '--untag', 'absent', code=1)
        self.assertEqual(path.read_bytes(), before)
        self.cli('modify', 'bug-a', 'summary=changed', '--comment', '   ', code=1)
        self.assertEqual(path.read_bytes(), before)
        self.cli('modify', 'bug-a', '--tag', 'forbidden', code=1)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(json.loads(self.cli('ls', 'open', '--output', 'json')), [])

    def test_metadata_and_noop_preserve_updated(self):
        self.create()
        store = ticket.Store(self.cwd / 'tickets')
        t = store.load('bug-a')
        t['updated'] = '2000-01-01T00:00:00Z'
        store.save(t)
        self.cli('modify', 'bug-a', '--tag', 'urgent', '--comment', 'note')
        self.cli('comment', 'bug-a', 'another note')
        self.cli('tag', 'bug-a', 'easy')
        self.assertEqual(self.load()['updated'], t['updated'])
        path = store.path('bug-a')
        before = path.read_bytes()
        self.assertIn('nothing to modify', self.cli('modify', 'bug-a', 'priority=+00', '--tag', 'urgent'))
        self.assertEqual(path.read_bytes(), before)
        self.cli('modify', 'bug-a', 'priority=2')
        self.assertNotEqual(self.load()['updated'], t['updated'])

    def test_links_reverse_names_and_partial_unlink(self):
        self.configure()
        self.create()
        self.create('bug-b')
        self.create('bug-c')
        self.cli('modify', 'bug-a', '--link', 'bug-b:blocks', '--link', 'bug-c:blocks', '--link', 'bug-b:blocks')
        self.assertEqual(self.load()['relationships'], {'blocks': ['bug-b', 'bug-c']})
        shown = json.loads(self.cli('show', 'bug-b', '--output', 'json'))
        self.assertEqual(shown['referenced_by'][0]['reverse_name'], 'blocked-by')
        self.cli('modify', 'bug-a', '--unlink', 'bug-b:blocks')
        self.assertEqual(self.load()['relationships'], {'blocks': ['bug-c']})
        self.cli('modify', 'bug-a', '--link', 'missing:blocks', code=1)
        self.cli('modify', 'bug-a', '--link', 'bug-b:unknown', code=1)
        self.assertEqual(self.load()['relationships'], {'blocks': ['bug-c']})
        self.cli('modify', 'bug-a', '--unlink', 'bug-c')
        self.assertEqual(self.load()['relationships'], {})

    def test_reverse_relationship_declaration(self):
        self.configure()
        self.create()
        self.create('bug-b')
        self.cli('modify', 'bug-b', '--link', 'bug-a:blocked-by')
        self.assertEqual(self.load('bug-b')['relationships'], {'blocked-by': ['bug-a']})

    def test_type_change_revalidates_status_and_links(self):
        self.configure()
        self.create()
        before = (self.cwd / 'tickets/bug-a.yaml').read_bytes()
        self.cli('modify', 'bug-a', 'type=feat', code=1)
        self.assertEqual((self.cwd / 'tickets/bug-a.yaml').read_bytes(), before)
        self.cli('modify', 'bug-a', 'type=feat', 'status=implemented')
        self.assertEqual(self.load()['type'], 'feat')

    def test_search_and_status_shorthand(self):
        self.configure()
        self.create()
        self.create('bug-b', 'bug', '--status', 'fixed')
        rows = json.loads(self.cli('search', 'CAFÉ', 'startup', '--min-score', '1', '--filter', 'open', '--output', 'json'))
        self.assertEqual([t['id'] for t in rows], ['bug-a'])
        self.assertEqual(len(json.loads(self.cli('ls', 'open', '--output', 'json'))), 1)
        self.cli('search', 'x', '--min-score', 'nan', code=1)
        self.cli('search', 'x', '--fields', 'priority', code=1)
        self.assertEqual(json.loads(self.cli('ls', 'nonsense', '--output', 'json')), [])

    def test_status_shorthand_works_without_workflow_configuration(self):
        self.create('bug-a', 'bug', '--status', 'open')
        self.create('bug-b', 'bug', '--status', 'in-progress')
        self.create('feat-a', 'feat', '--status', 'open')
        for status in ('open', 'in-progress', 'not-a-state'):
            with self.subTest(status=status):
                shorthand = json.loads(self.cli('ls', status, '--output', 'json'))
                explicit = json.loads(self.cli('ls', 'status=' + status, '--output', 'json'))
                self.assertEqual(shorthand, explicit)
        rows = json.loads(self.cli('ls', 'open', 'type=bug', '--output', 'json'))
        self.assertEqual([t['id'] for t in rows], ['bug-a'])
        rows = json.loads(self.cli('search', 'startup', '--filter', 'in-progress', '--output', 'json'))
        self.assertEqual([t['id'] for t in rows], ['bug-b'])

    def test_command_names_require_explicit_status_filters(self):
        self.create()
        for command in ticket.COMMAND_NAMES:
            with self.subTest(command=command):
                self.cli('modify', 'bug-a', 'status=' + command)
                result = subprocess.run([sys.executable, SCRIPT, 'ls', command], cwd=self.cwd, env=self.env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertIn('built-in command name', result.stderr)
                self.assertIn('status=' + command, result.stderr)
                rows = json.loads(self.cli('ls', 'status=' + command, '--output', 'json'))
                self.assertEqual([t['id'] for t in rows], ['bug-a'])
        self.cli('search', 'startup', '--filter', 'show', code=1)
        self.cli('modify', 'bug-a', 'status=show')
        rows = json.loads(self.cli('search', 'startup', '--filter', 'status=show', '--output', 'json'))
        self.assertEqual([t['id'] for t in rows], ['bug-a'])

    def test_open_option_is_removed_from_parser_and_help(self):
        self.create()
        self.cli('ls', '--open', code=2)
        self.assertNotIn('--open', self.cli('ls', '--help'))
        self.assertNotIn('--open', self.cli('config'))

    def test_directory_precedence_and_missing_explicit_directory(self):
        self.cli('-d', 'missing', 'ls', code=1)
        (self.cwd / 'configured').mkdir()
        (self.cwd / 'environment').mkdir()
        (self.cwd / 'cli').mkdir()
        (self.cwd / '.ticketconfig.yaml').write_text('version: v0.5\nbase-directory: configured\n')
        self.create()
        self.assertTrue((self.cwd / 'configured/bug-a.yaml').exists())
        self.env['TICKET_BASE_DIR'] = 'environment'
        self.create()
        self.assertTrue((self.cwd / 'environment/bug-a.yaml').exists())
        self.cli('create', '-d', 'cli', 'bug', 'hello', '--overwrite-id', 'bug-a')
        self.assertTrue((self.cwd / 'cli/bug-a.yaml').exists())

    def test_verify_reports_all_and_list_skips_corrupt(self):
        self.create()
        store = ticket.Store(self.cwd / 'tickets')
        t = store.load('bug-a')
        t.update(id='wrong', created='bad', updated='bad', relationships={'blocks': ['missing']})
        store.path('bug-a').write_text(ticket.dump_yaml(ticket.ticket_value(t)))
        (self.cwd / 'tickets/corrupt.yaml').write_text('bad: [')
        issues = json.loads(self.cli('verify', '--output', 'json', code=1))
        self.assertEqual(len(issues), 5)
        rows = json.loads(self.cli('ls', '--output', 'json'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], 'wrong')

    def test_id_safety_and_yaml_suffix(self):
        for identifier in ('../escape', '..\\escape', '.', '..', ''):
            self.cli('create', 'bug', 's', '--overwrite-id', identifier, code=1)
        self.create('foo.yaml')
        self.assertTrue((self.cwd / 'tickets/foo.yaml.yaml').exists())
        self.assertEqual(json.loads(self.cli('ls', '--output', 'json'))[0]['id'], 'foo.yaml')
        before = (self.cwd / 'tickets/foo.yaml.yaml').read_bytes()
        self.cli('create', 'bug', 's', '--overwrite-id', 'foo.yaml', code=1)
        self.assertEqual((self.cwd / 'tickets/foo.yaml.yaml').read_bytes(), before)

    def test_output_formats_describe_the_same_tickets(self):
        self.configure()
        self.create()
        self.create('bug-b')
        self.cli('modify', 'bug-a', '--link', 'bug-b:blocks')
        self.cli('comment', 'bug-a', '--author', 'Alice', 'A note')
        commands = [
            ['overview'], ['ls', '--limit', '0'],
            ['ls', 'open'], ['show', 'bug-a'], ['show', 'bug-b'],
            ['search', 'startup'], ['verify'],
        ]
        for command in commands:
            with self.subTest(command=command):
                value = json.loads(self.cli(*command, '--output', 'json'))
                yaml_value = ticket.yaml.load(self.cli(*command, '--output', 'yaml'), Loader=ticket.Loader)
                self.assertEqual(value, yaml_value)
        self.assertIn('Café startup crash', self.cli('ls'))
        self.assertIn('  <- a (blocked-by)', self.cli('show', 'bug-b'))
        self.assertIn('Config schema:', self.cli('overview'))

    def test_bare_unlink_removes_matching_relationship_entries(self):
        self.create()
        self.create('bug-b')
        self.create('bug-c')
        self.cli('modify', 'bug-a', '--link', 'bug-b:blocks', '--link', 'bug-c:blocks', '--link', 'bug-c:related')
        self.cli('modify', 'bug-a', '--unlink', 'bug-b')
        self.assertEqual(self.load()['relationships'], {'related': ['bug-c']})

    def test_interspersed_options_and_help(self):
        self.create()
        self.cli('modify', 'bug-a', '--tag', 'easy', 'priority=3', '--comment', 'ok', 'status=open')
        self.assertEqual(self.load()['priority'], 3)
        self.assertIn('usage:', self.cli('modify', '--help'))
        self.assertIn('0.5.0', self.cli('-V'))


class ModelTests(unittest.TestCase):
    def test_semver_format_parsing(self):
        for value in ('v0.5', '0.5', '0.5.0', 'v0.5.0'):
            self.assertEqual(ticket.FormatVersion.parse(value), ticket.FormatVersion(0, 5, 0))
        parsed = ticket.FormatVersion.parse('v12.34.56-alpha.1+build.007')
        self.assertEqual(parsed, ticket.FormatVersion(12, 34, 56, 'alpha.1', 'build.007'))
        invalid = [None, 0.5, True, [], {}, '', 'v0', 'v00.5', '0.05.0', '0.5.01',
                   '0.5.0.1', '0.5.-1', '0.5.0-', '0.5.0-alpha..1', '0.5.0-01',
                   '0.5.0+', '0.5.0+build..1', '0.5.0+ä', ' 0.5.0', '0.5.0\n']
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ticket.TicketError):
                ticket.FormatVersion.parse(value)

    def test_semver_breaking_change_boundaries(self):
        pairs = [('0.5.0', '0.5.99', True), ('0.5.0', '0.4.9', False),
                 ('0.5.0', '0.6.0', False), ('0.5.0', '1.0.0', False),
                 ('1.2.3', '1.9.0', True), ('1.2.3', '2.2.3', False),
                 ('0.5.0', '0.5.0+build.1', True),
                 ('0.5.0', '0.5.0-rc.1', False),
                 ('0.5.0-rc.1', '0.5.0-rc.1+build', True),
                 ('0.5.0-rc.1', '0.5.0-rc.2', False)]
        for left, right, expected in pairs:
            with self.subTest(left=left, right=right):
                a, b = ticket.FormatVersion.parse(left), ticket.FormatVersion.parse(right)
                self.assertEqual(a.compatible_with(b), expected)
                self.assertEqual(b.compatible_with(a), expected)

    def test_display_handles_custom_ids_and_exact_id_shadowing(self):
        ids = ['bug-abcd123456789012', 'abcd', 'release', 'bug-a', 'a']
        display = ticket.IdDisplay(ids)
        self.assertEqual(display(ids[0]), 'abcd1')
        self.assertEqual(display('abcd'), 'abcd')
        self.assertEqual(display('release'), 'rele')
        self.assertEqual(display('a'), 'a')
        self.assertEqual(display('bug-a'), 'bug-a')
        for identifier in ids:
            self.assertEqual(ticket.matching_ids(display(identifier), ids), [identifier])

    def test_example_configuration(self):
        config = ticket.Config(ticket.read_yaml(FIXTURES / 'config.yaml'))
        self.assertTrue(config.allows_type('feat'))
        config.check_status('rev', 'in-progress')
        config.check_relationship('feat', 'released-in', 'ver')
        self.assertEqual(config.reverse_name('feat', 'released-in'), 'contains')

    def test_config_errors(self):
        invalid = [
            {'types': ['bug', 'bug']},
            {'types': [{'bug': {'initial-state': 'new', 'states': ['new', 'new']}}]},
            {'types': [{'bug': {'initial-state': 'new', 'states': []}}]},
            {'tags': 'urgent'}, {'types': {}},
            {'types': [{'bug': {'initial-state': 'new', 'states': [{'new': {'typo': []}}]}}]},
            {'types': [{'bug': {'initial-state': 'new', 'states': ['new'], 'relationships': [{'r': None}, {'r': None}]}}]},
        ]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ticket.TicketError):
                ticket.Config(dict(version='v0.5', **raw))

    def test_yaml_scalars_and_duplicate_keys(self):
        raw = ticket.yaml.load('status: on\ncreated: 2026-01-01T00:00:00Z\n', Loader=ticket.Loader)
        self.assertEqual(raw['status'], 'on')
        self.assertIsInstance(raw['created'], str)
        with self.assertRaises(ticket.TicketError):
            ticket.yaml.load('id: a\nid: b\n', Loader=ticket.Loader)

    def test_sort_offsets_nanoseconds_invalid_and_ties(self):
        a = dict(id='a', created='2026-01-01T23:00:00+02:00')
        b = dict(id='b', created='2026-01-01T22:00:00Z')
        self.assertEqual([t['id'] for t in ticket.sort_tickets([a, b], 'new')], ['b', 'a'])
        c = dict(id='c', created='bad')
        self.assertEqual(ticket.sort_tickets([a, c], 'old')[0]['id'], 'c')
        a['created'] = '2026-01-01T22:00:00.000000001Z'
        b['created'] = '2026-01-01T22:00:00.000000002Z'
        self.assertEqual(ticket.sort_tickets([a, b], 'new')[0]['id'], 'b')
        a['created'] = '2026-01-01T23:00:00+01:00'
        b['created'] = '2026-01-01T22:00:00Z'
        self.assertEqual(ticket.sort_tickets([b, a], 'new')[0]['id'], 'a')

    def test_indel_score_matches_slow_lcs(self):
        def slow(a, b):
            row = [0] * (len(b) + 1)
            for x in a:
                prev = row[:]
                for j, y in enumerate(b, 1):
                    row[j] = prev[j-1] + 1 if x == y else max(prev[j], row[j-1])
            return 2 * row[-1] / (len(a) + len(b)) if a or b else 1
        words = ['', 'a', 'b', 'ab', 'ba', 'abc', 'aabc', 'Café']
        for a, b in itertools.product(words, repeat=2):
            self.assertEqual(ticket.ratio(a, b), slow(a, b))
        self.assertEqual(ticket.score(' HELLO\nworld ', 'hello world'), 1)

    def test_relationship_shapes(self):
        t = dict(id='a', summary='s', description='', author='', assignee='', approver='', type='bug', status='new', priority=0, created='x', updated='x')
        for rels in ([], {'r': []}, {'r': ['a', 'a']}, {'r': [1]}):
            with self.subTest(rels=rels), self.assertRaises(ticket.TicketError):
                ticket.parse_ticket(dict(t, relationships=rels))
        parsed = ticket.parse_ticket(dict(t, relationships={'r': 'b'}))
        self.assertEqual(parsed['relationships'], {'r': ['b']})
        self.assertEqual(parsed['tags'], [])
        self.assertEqual(ticket.ticket_value(parsed)['relationships'], {'r': 'b'})


if __name__ == '__main__':
    unittest.main()
