"""Local approval tests: no API/model or phone calls."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chelys import approval, cli, telegram, tools
from chelys.agent import Agent
from chelys.store import Store
from tests.test_agent import Base, make_settings


class ToolApprovalTests(Base):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.s = make_settings(self.tmp)
        self.store = Store(Path(':memory:'))
        self.sid = self.store.new_session()
        self.asked = []

    def action(self, name, args, answer=approval.DENY, sid=None, **kw):
        def approve(req):
            self.asked.append(req)
            return answer
        a = Agent(self.s, 'unused', self.store, sid or self.sid, approve, **kw)
        return a._execute(name, args)

    def test_always_matches_exact_action_not_purpose_across_disk_reopen(self):
        self.store.close()
        self.store = Store(self.s.db_path)
        self.sid = self.store.new_session()
        args = {'command': 'printf ok', 'purpose': 'first reason'}
        self.assertFalse(self.action('run_shell', args, approval.ALWAYS)[1])
        self.store.close()
        self.store = Store(self.s.db_path)
        other = self.store.new_session()
        self.assertFalse(self.action('run_shell', dict(args, purpose='new reason'), sid=other)[1])
        self.assertEqual(len(self.asked), 1)
        self.assertTrue(self.action('run_shell', dict(args, command='printf changed'))[1])
        sub = self.tmp / 'sub'
        sub.mkdir()
        self.s.workdir = str(sub)
        self.assertTrue(self.action('run_shell', args)[1])
        self.s.workdir = str(self.tmp)
        write = {'path': 'out', 'content': 'hello'}
        self.assertFalse(self.action('write_file', write, approval.ALWAYS)[1])
        self.assertFalse(self.action('write_file', dict(write, path=str(self.tmp / 'out'), purpose='why'))[1])
        self.assertTrue(self.action('write_file', dict(write, content='changed'))[1])
        self.assertTrue(self.action('write_file', dict(write, path='different'))[1])
        self.s.workdir = str(sub)
        self.assertTrue(self.action('write_file', write)[1])

    def test_guest_cannot_use_grant_or_approve_risky_tools(self):
        args = {'command': 'printf ok'}
        self.action('run_shell', args, approval.SESSION)
        for name, args in [('run_shell', args), ('write_file', {'path': 'guest', 'content': 'no'})]:
            out, failed = self.action(name, args, approval.ALWAYS, guest=True)
            self.assertTrue(failed)
            self.assertTrue(out.startswith('denied:'))
        self.assertEqual(len(self.asked), 1)
        self.assertFalse((self.tmp / 'guest').exists())

    def test_ask_requires_allow_risky_even_with_saved_conversation_grant(self):
        import contextlib
        import io
        from tests.test_agent import FakeModel, tool_call
        self.store.close()
        self.store = Store(self.s.db_path)
        self.sid = self.store.new_session()
        self.action('run_shell', {'command': 'printf ok'}, approval.SESSION)
        args = cli.build_parser().parse_args(['ask', '--session', str(self.sid), 'test'])
        def make_agent(*pos, **kw):
            model = FakeModel([
                {'role': 'assistant', 'content': None, 'tool_calls': [tool_call('write_file', {'path': 'ask-out', 'content': 'no'})]},
                {'role': 'assistant', 'content': 'done'}])
            return Agent(*pos, **kw, chat_fn=model)
        with patch.object(cli, 'load_api_key', return_value='unused'), patch.object(cli, 'Agent', make_agent), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_ask(self.s, args), 0)
            self.assertFalse((self.tmp / 'ask-out').exists())
            args.allow_risky = True
            self.assertEqual(cli.cmd_ask(self.s, args), 0)
            self.assertEqual((self.tmp / 'ask-out').read_text(), 'no')

    def test_ordinary_cards_offer_four_choices_with_action_label_in_both_uis(self):
        import contextlib
        import io
        from tests.test_telegram import FakeBot, OWNER, press
        factory = telegram.AgentFactory(self.s, 'unused', self.store, OWNER)
        for name, args in [('run_shell', {'command': 'printf ok'}), ('write_file', {'path': 'out', 'content': 'hi'})]:
            req = tools.describe_call(name, args)
            self.assertEqual(req.choices, (approval.ONCE, approval.SESSION, approval.ALWAYS, approval.DENY))
            bot = FakeBot([press(OWNER, 'al')])
            bridge = telegram.Bridge(self.s, bot, OWNER, factory, log=lambda s: None)
            self.assertEqual(bridge.approve(OWNER, req), approval.ALWAYS)
            completed = [p.get('text', '') for method, p in bot.sent if method in ('answerCallbackQuery', 'editMessageText')]
            self.assertTrue(completed)
            self.assertTrue(all('this action' in text and 'this skill' not in text for text in completed))
            buttons = next(p['reply_markup']['inline_keyboard'] for m, p in bot.sent if 'reply_markup' in p)
            self.assertEqual([b['text'] for row in buttons for b in row], ['Allow once', 'Allow for this conversation', 'Always this action', 'Deny'])
            output = io.StringIO()
            with patch('builtins.input', side_effect=lambda prompt: (output.write(prompt), 'a')[1]), contextlib.redirect_stdout(output):
                self.assertEqual(cli._ask_permission(req), approval.ALWAYS)
            self.assertIn('this action', output.getvalue())
            self.assertNotIn('this skill', output.getvalue())
            with patch('builtins.input', side_effect=EOFError), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli._ask_permission(req), approval.DENY)

    def test_list_revoke_clear_ordinary_grants_without_touching_skills(self):
        self.store.grant('test-skill', 0)
        self.action('run_shell', {'command': 'printf private-secret'}, approval.ALWAYS)
        self.action('write_file', {'path': 'out', 'content': 'hello'}, approval.SESSION)
        listing = approval.manage(self.store, '')
        self.assertIn('run_shell', listing)
        self.assertIn(f'conversation #{self.sid}', listing)
        self.assertNotIn('private-secret', listing)
        rows = self.store.tool_grants()
        self.assertEqual(len(rows), 2)
        exact = next(r for r in rows if r['session_id'] == 0)
        self.assertIn('Revoked', approval.manage(self.store, f"revoke {exact['id']}"))
        self.assertTrue(self.action('run_shell', {'command': 'printf private-secret'}, sid=self.store.new_session())[1])
        self.assertIn('Cleared', approval.manage(self.store, 'clear'))
        self.assertEqual(self.store.tool_grants(), [])
        self.assertIn('No ordinary', approval.manage(self.store, ''))
        self.assertTrue(self.action('run_shell', {'command': 'printf ok'})[1])
        self.assertEqual(self.store.always_grants(), ['test-skill'])
        self.assertIn('Usage:', approval.manage(self.store, 'revoke invalid'))
        self.assertIn('No such', approval.manage(self.store, 'revoke 999'))

    def test_cli_lists_and_revokes_without_api_and_chat_handles_approvals(self):
        import contextlib
        import io
        self.store.close()
        self.store = Store(self.s.db_path)
        self.sid = self.store.new_session()
        self.action('run_shell', {'command': 'printf ok'}, approval.ALWAYS)
        output = io.StringIO()
        args = cli.build_parser().parse_args(['approvals'])
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.cmd_approvals(self.s, args), 0)
        self.assertIn('run_shell', output.getvalue())
        gid = self.store.tool_grants()[0]['id']
        output = io.StringIO()
        args = cli.build_parser().parse_args(['chat', '--session', str(self.sid)])
        with patch.object(cli, 'load_api_key', return_value='unused'), patch('builtins.input', side_effect=['/approvals', f'/approvals revoke {gid}', '/quit']), contextlib.redirect_stdout(output):
            self.assertEqual(cli.cmd_chat(self.s, args), 0)
        self.assertIn('run_shell', output.getvalue())
        self.assertIn('Revoked', output.getvalue())
        self.assertEqual(self.store.tool_grants(), [])
        self.action('run_shell', {'command': 'printf ok'}, approval.SESSION)
        args = cli.build_parser().parse_args(['approvals', 'clear'])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_approvals(self.s, args), 0)
        self.assertEqual(self.store.tool_grants(), [])

    def test_telegram_management_is_owner_only(self):
        from tests.test_telegram import FakeBot, OWNER, GUEST, msg
        self.action('run_shell', {'command': 'printf ok'}, approval.ALWAYS)
        gid = self.store.tool_grants()[0]['id']
        factory = telegram.AgentFactory(self.s, 'unused', self.store, OWNER)
        bot = FakeBot([])
        bridge = telegram.Bridge(self.s, bot, OWNER, factory, log=lambda s: None, guests={GUEST})
        for text in ['/approvals', f'/approvals revoke {gid}', '/approvals clear']:
            bridge.handle(msg(1, GUEST, text)['message'])
        self.assertEqual(len(self.store.tool_grants()), 1)
        self.assertTrue(all('owner only' in t for t in bot.texts()))
        bridge.handle(msg(2, OWNER, '/approvals')['message'])
        self.assertIn('run_shell', bot.texts()[-1])
        bridge.handle(msg(3, OWNER, f'/approvals revoke {gid}')['message'])
        self.assertIn('Revoked', bot.texts()[-1])
        self.assertEqual(self.store.tool_grants(), [])
        self.action('run_shell', {'command': 'printf ok'}, approval.SESSION)
        bridge.handle(msg(4, OWNER, '/approvals clear')['message'])
        self.assertIn('Cleared', bot.texts()[-1])
        self.assertEqual(self.store.tool_grants(), [])

    def test_once_and_deny_never_persist_and_default_still_asks(self):
        args = {'path': 'out', 'content': 'hello'}
        for answer in (approval.ONCE, True, approval.DENY, False, 'unknown'):
            with self.subTest(answer=answer):
                self.action('write_file', args, answer)
                self.assertEqual(self.store.tool_grants(), [])
        before = len(self.asked)
        self.assertTrue(self.action('write_file', args)[1])
        self.assertTrue(self.action('write_file', args)[1])
        self.assertEqual(len(self.asked), before + 2)
        self.store.grant('*', self.sid)
        self.assertTrue(self.action('write_file', args)[1], 'skill conversation grants never authorize ordinary tools')

    def test_background_review_does_not_reuse_owner_action_grants(self):
        args = {'command': 'printf ok'}
        self.action('run_shell', args, approval.ALWAYS)
        self.assertTrue(self.action('run_shell', args, actor='review')[1])
        self.assertEqual(len(self.asked), 2)

    def test_conversation_grant_survives_new_agent_and_covers_both_tools(self):
        self.assertFalse(self.action('run_shell', {'command': 'printf ok'}, approval.SESSION)[1])
        self.assertFalse(self.action('write_file', {'path': 'out', 'content': 'hello'})[1])
        self.assertEqual(len(self.asked), 1)
        self.assertEqual((self.tmp / 'out').read_text(), 'hello')
        other = self.store.new_session()
        self.assertTrue(self.action('run_shell', {'command': 'printf ok'}, sid=other)[1])
        self.assertEqual(len(self.asked), 2)
