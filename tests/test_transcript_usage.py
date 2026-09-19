import json
import pathlib
import tempfile
import unittest

from transcript_model import SourceIndex
from transcript_usage import counts


class UsageTests(unittest.TestCase):
    def index(self, harness, rows):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = pathlib.Path(temporary.name) / 'source.jsonl'
        path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
        return SourceIndex(harness, path)

    def test_claude_repeated_blocks_count_once_and_tool_results_are_not_user_turns(self):
        usage = {'input_tokens': 100, 'cache_read_input_tokens': 8000,
                 'cache_creation_input_tokens': 1000, 'output_tokens': 900,
                 'output_tokens_details': {'thinking_tokens': 600}}
        rows = [{'type': 'user', 'uuid': 'u1', 'message': {'content': 'First request'}}]
        for i, block in enumerate([{'type': 'thinking', 'thinking': 'summary'}, {'type': 'text', 'text': 'Working'}, {'type': 'tool_use', 'name': 'Bash', 'id': 'tool', 'input': {'command': 'pwd'}}]):
            rows.append({'type': 'assistant', 'uuid': f'a{i}', 'message': {'id': 'msg1', 'usage': usage, 'content': [block]}})
        rows += [{'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'tool', 'content': 'ok'}]}},
                 {'type': 'user', 'uuid': 'u2', 'message': {'content': 'Next request'}},
                 {'type': 'assistant', 'uuid': 'b', 'message': {'id': 'msg2', 'content': [{'type': 'text', 'text': 'No usage'}]}}]
        index = self.index('claude', rows)
        data = index.usage.public(1)
        self.assertEqual(len(data['turns']), 2)
        self.assertEqual(data['selected']['steps'], 1)
        self.assertEqual(data['selected']['tool_calls'], 1)
        self.assertEqual(data['selected']['totals']['input'], 9100)
        self.assertEqual(data['selected']['totals']['uncached'], 1100)
        self.assertEqual(data['steps'][0]['reasoning'], 600)
        self.assertEqual(data['steps'][0]['other_output'], 300)
        unknown = index.usage.public(6)
        self.assertEqual(unknown['selected']['known']['input'], 0)
        self.assertIsNone(unknown['steps'][0]['input'])

    def test_codex_checkpoint_repeats_and_counter_reset(self):
        def event(total, last):
            return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': total, 'last_token_usage': last}}}
        first = {'input_tokens': 8000, 'cached_input_tokens': 4000, 'output_tokens': 1000, 'reasoning_output_tokens': 600}
        second = {'input_tokens': 9000, 'cached_input_tokens': 8000, 'output_tokens': 700, 'reasoning_output_tokens': 200}
        cumulative = {key: first[key]+second[key] for key in first}
        rows = [{'type': 'session_meta', 'payload': {'id': 's'}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Prompt'}]}},
                event(first, first), event(first, first), event(cumulative, second), event(first, first)]
        data = self.index('codex', rows).usage.public(2)
        self.assertEqual(len(data['steps']), 3)
        self.assertEqual(data['steps'][0]['uncached'], 4000)
        self.assertEqual(data['steps'][1]['other_output'], 500)

    def test_unknown_does_not_become_zero_and_cumulative_delta_is_labeled(self):
        self.assertIsNone(counts({'input_tokens': 100}, 'claude')['input'])
        self.assertIsNone(counts({'output_tokens': 100}, 'codex')['reasoning'])
        rows = [{'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {'input_tokens': n, 'output_tokens': n}}}} for n in (100, 200)]
        data = self.index('codex', rows).usage.public(1)
        self.assertEqual(len(data['steps']), 1)
        self.assertEqual(data['steps'][0]['input'], 100)
        self.assertIn('may span calls', data['steps'][0]['basis'])

    def test_fork_retimestamped_parent_usage_is_visible_but_excluded_from_totals(self):
        born = '2026-07-11T12:12:05.540Z'
        first = {'input_tokens': 20188, 'cached_input_tokens': 9984, 'output_tokens': 424, 'reasoning_output_tokens': 254}
        second = {'input_tokens': 25000, 'cached_input_tokens': 24000, 'output_tokens': 100, 'reasoning_output_tokens': 40}
        receipt = lambda u: {'type': 'event_msg', 'timestamp': born, 'payload': {'type': 'token_count', 'info': {'total_token_usage': u, 'last_token_usage': u}}}
        rows = [{'type': 'session_meta', 'timestamp': born, 'payload': {'id': 'child', 'parent_thread_id': 'parent', 'timestamp': born}},
                {'type': 'session_meta', 'timestamp': born, 'payload': {'id': 'parent'}},
                {'type': 'event_msg', 'timestamp': born, 'payload': {'type': 'task_started', 'started_at': 1783665296}},
                receipt(first),
                {'type': 'event_msg', 'timestamp': born, 'payload': {'type': 'task_started', 'started_at': 1783771925}},
                receipt(second)]
        index = self.index('codex', rows)
        parent = index.usage.public(4)
        self.assertEqual(parent['selected']['steps'], 0)
        self.assertEqual(parent['selected']['inherited_steps'], 1)
        self.assertEqual(parent['selected']['totals']['output'], 0)
        self.assertEqual(parent['steps'][0]['output'], 424)
        child = index.usage.public(6)
        self.assertEqual(child['selected']['totals']['output'], 100)
        self.assertEqual(child['selected']['steps'], 1)
        self.assertEqual(child['selected']['title'], 'Agent task')

    def test_explicit_response_owner_beats_unknown_fork_chronology(self):
        rows = [{'type': 'session_meta', 'payload': {'id': 'child', 'parent_thread_id': 'parent'}}]
        for owner in ('parent', 'child'):
            rows.append({'type': 'token_usage_record', 'payload': {'thread_id': owner, 'response_id': owner, 'usage': {'input_tokens': 100, 'output_tokens': 10}}})
        data = self.index('codex', rows).usage.public(1)
        self.assertEqual(data['selected']['steps'], 1)
        self.assertEqual(data['selected']['inherited_steps'], 1)
        self.assertEqual(data['selected']['totals']['output'], 10)

    def test_migration_synthetic_start_does_not_reassign_parent_usage(self):
        usage = {'input_tokens': 20188, 'output_tokens': 424}
        rows = [{'type': 'session_meta', 'payload': {'id': 'child', 'parent_thread_id': 'parent', 'timestamp': '2026-07-11T12:12:05.437Z'}},
                {'type': 'turn_context', 'payload': {'turn_id': '019f50b8-52c4-7403-afb4-ec28e5cac3d1'}},
                {'type': 'event_msg', 'payload': {'type': 'task_started', 'started_at': 1783771925, 'turn_id': 'rollout-4'}},
                {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': usage, 'last_token_usage': usage}}}]
        data = self.index('codex', rows).usage.public(1)
        self.assertEqual(data['selected']['steps'], 0)
        self.assertEqual(data['selected']['inherited_steps'], 1)

    def test_response_id_receipt_wins_over_token_count_mirror_and_badges_share_totals(self):
        from transcript_model import transcript_page
        usage = {'input_tokens': 8000, 'cached_input_tokens': 4000, 'output_tokens': 1000, 'reasoning_output_tokens': 600}
        rows = [{'type': 'session_meta', 'payload': {'id': 's'}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '<environment_context>metadata</environment_context>'}]}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Real prompt'}]}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Working'}]}},
                {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'Bash', 'call_id': 'call', 'arguments': '{"command":"pwd"}'}},
                {'type': 'token_usage_record', 'payload': {'response_id': 'response-one', 'usage': usage, 'thread_token_usage': usage}},
                {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'last_token_usage': usage, 'total_token_usage': usage}}}]
        index = self.index('codex', rows)
        data = index.usage.public(3)
        self.assertEqual(len(data['turns']), 1)
        self.assertEqual(len(data['steps']), 1)
        self.assertEqual(data['steps'][0]['identity'], 'response:response-one')
        items = transcript_page(index, 1, 20)['items']
        badges = [item['usage'] for item in items if item.get('usage')]
        self.assertEqual(len(badges), 2)
        self.assertEqual(badges[0]['identity'], badges[1]['identity'])
        self.assertEqual(badges[0]['input'], 8000)


if __name__ == '__main__':
    unittest.main()
