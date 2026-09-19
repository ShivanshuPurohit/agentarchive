import base64
import http.client
import http.server
import json
import pathlib
import tempfile
import threading
import types
import unittest

from transcript_media import assets_in, load_asset
from transcript_model import IndexCache, SourceIndex, transcript_page
from transcript_viewer import Source, _handler


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.config = {'excluded_workspaces': [str(self.project / 'private')]}
        self.path = self.root / 'source.jsonl'

    def index(self, content, harness='claude'):
        if harness == 'claude':
            records = [{'type': 'user', 'cwd': str(self.project), 'sessionId': 's', 'message': {'content': content}}]
        else:
            records = [{'type': 'session_meta', 'payload': {'id': 's', 'cwd': str(self.project)}},
                       {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'a', 'output': json.dumps({'content': content})}}]
        self.path.write_text('\n'.join(json.dumps(r) for r in records) + '\n')
        return SourceIndex(harness, self.path)

    def test_embedded_media_is_lazy_and_source_scoped_in_both_harnesses(self):
        encoded = base64.b64encode(b'example image').decode()
        for harness in ('claude', 'codex'):
            with self.subTest(harness=harness):
                index = self.index([{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': encoded}}], harness)
                page = transcript_page(index, 1, 20)
                self.assertNotIn(encoded, json.dumps(page))
                line = 1 if harness == 'claude' else 2
                mime, raw = load_asset(self.config, index, line, 0, 0)
                self.assertEqual((mime, raw), ('image/png', b'example image'))

    def test_project_local_reference_and_active_document_preview(self):
        (self.project / 'report.html').write_text('<h1>Result</h1>')
        index = self.index([{'type': 'text', 'text': '![Report](report.html)'}])
        mime, raw = load_asset(self.config, index, 1, 0, 0)
        self.assertEqual(mime, 'text/plain; charset=utf-8')
        self.assertIn(b'<h1>', raw)

    def test_multiple_blocks_in_one_event_are_not_treated_as_duplicate_events(self):
        from transcript_model import transcript_page
        blocks = [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'aGk='}},
                  {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': 'aGk='}}]
        self.path.write_text(json.dumps({'type': 'user', 'uuid': 'same-event', 'message': {'content': blocks}}) + '\n')
        index = SourceIndex('claude', self.path)
        items = transcript_page(index, 1, 20)['items']
        self.assertEqual([item['visibility'] for item in items], ['normal', 'normal'])
        self.assertFalse(index.duplicates)

    def test_exclusions_traversal_symlinks_and_remote_refs_fail_closed(self):
        outside = self.root / 'secret.png'
        outside.write_bytes(b'private')
        (self.project / 'link.png').symlink_to(outside)
        private = self.project / 'private'
        private.mkdir()
        (private / 'a.png').write_bytes(b'incognito')
        for ref in ('../secret.png', 'link.png', 'private/a.png', 'https://example.com/a.png', 'missing.png'):
            with self.subTest(ref=ref):
                index = self.index([{'type': 'text', 'text': f'![Image]({ref})'}])
                with self.assertRaises(FileNotFoundError):
                    load_asset(self.config, index, 1, 0, 0)

    def test_opaque_envelopes_not_inspected_and_invalid_base64_rejected(self):
        self.assertEqual(assets_in({'schema': {'type': {'enum': ['string', 'null']}}}), [])
        self.assertEqual(assets_in({'signature': '![x](a.png)', 'encrypted_content': '![x](b.png)'}), [])
        index = self.index([{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'invalid?'}}])
        with self.assertRaises(ValueError):
            load_asset(self.config, index, 1, 0, 0)
        with self.assertRaises(FileNotFoundError):
            load_asset(self.config, index, 1, 0, 30)

    def test_http_media_requires_capability_and_does_not_accept_file_paths(self):
        index = self.index([{'type': 'image', 'image_url': 'data:image/png;base64,aGk='}], 'codex')
        source = Source('test', 'codex', self.path, 'source.jsonl', 's', 's')
        state = types.SimpleNamespace(config=self.config, cache=IndexCache(), lock=threading.RLock(), token='test-token', assets={},
                                      registry=types.SimpleNamespace(source=lambda key: source if key == 'test' else None))
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _handler(state))
        state.authority = f'127.0.0.1:{server.server_port}'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
        self.addCleanup(connection.close)
        path = '/api/transcripts/test/media?line=2&index=0&asset=0&path=/etc/passwd'
        connection.request('GET', path)
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        connection.request('GET', '/?token=test-token')
        response = connection.getresponse()
        self.assertEqual(response.status, 303)
        cookie = response.getheader('Set-Cookie').split(';', 1)[0]
        response.read()
        self.assertTrue(cookie.startswith(f'agent_archive_viewer_{server.server_port}='))
        # Browsers share cookies across ports; another viewer cannot overwrite this one.
        connection.request('GET', path, headers={'Cookie': f'agent_archive_viewer_1=other; {cookie}'})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b'hi')
        self.assertEqual(response.getheader('X-Content-Type-Options'), 'nosniff')


if __name__ == '__main__':
    unittest.main()
