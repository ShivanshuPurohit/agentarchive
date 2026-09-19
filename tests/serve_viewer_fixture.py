"""Manual browser acceptance fixture, through the production viewer HTTP handler.

Run `python3 tests/serve_viewer_fixture.py`; use the printed capability URL.
The temporary source is synthetic and disappears when the server exits.
"""
import base64
import http.server
import io
import json
import pathlib
import struct
import sys
import tempfile
import threading
import types
import wave
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from transcript_model import IndexCache
from transcript_viewer import Source, ViewerState, _handler


def png():
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    pixels = b''.join(b'\0' + bytes([70, 160, 200, 255]) * 200 for _ in range(80))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 200, 80, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')


def pdf():
    stream = b'BT /F1 24 Tf 30 120 Td (Retained PDF preview) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 420 180] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream']
    raw = b'%PDF-1.4\n'
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(raw))
        raw += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(raw)
    raw += b'xref\n0 6\n0000000000 65535 f \n' + b''.join(f'{p:010d} 00000 n \n'.encode() for p in offsets[1:])
    return raw + f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode()


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        path = root / 'synthetic.jsonl'
        wav = io.BytesIO()
        with wave.open(wav, 'wb') as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(8000); audio.writeframes(b'\0' * 1600)
        def media(kind, mime, data):
            return {'type': kind, 'source': {'type': 'base64', 'media_type': mime, 'data': base64.b64encode(data).decode()}}
        def record(kind, content, seconds, identity=None):
            return {'type': kind, 'uuid': str(seconds), 'cwd': str(root), 'sessionId': 'fixture', 'timestamp': f'2026-01-01T10:00:{seconds:02d}Z', 'message': {'id': identity, 'content': content, 'model': 'fixture-model', 'usage': {'input_tokens': 100, 'cache_creation_input_tokens': 1000, 'cache_read_input_tokens': 8000, 'output_tokens': 1000, 'output_tokens_details': {'thinking_tokens': 700}} if kind == 'assistant' else None}}
        text = '''## Rendered work
A **readable** paragraph, `inline code`, and [a link](https://example.com).

| Check | Result |
|---|---|
| Table | Rendered |

1. Nested code stays in its list:

   ```python
   print("hello")
   ```

```mermaid
flowchart LR
  Prompt --> Model --> Tool --> Model
```

```math
E = mc^2
```

```csv
step,input,output
1,8000,1000
2,9000,700
```

```json
{"detail":"collapsed until requested","steps":2}
```

```html
<h2>Sandboxed artifact</h2><p>A visible HTML result.</p><script>throw new Error("must not execute")</script>
```
'''
        rows = [record('user', [{'type': 'text', 'text': 'Verify rich content.'}], 0),
                record('assistant', [{'type': 'text', 'text': text}], 1, 'm1'),
                record('assistant', [{'type': 'tool_use', 'id': 'shell', 'name': 'Bash', 'input': {'command': 'printf progress'}}], 2, 'm1'),
                record('user', [{'type': 'tool_result', 'tool_use_id': 'shell', 'content': '\x1b[32mPASS\x1b[0m\nfirst\nsecond\x1b[1A\rreplaced'}], 4),
                record('assistant', [{'type': 'tool_use', 'id': 'edit', 'name': 'Edit', 'input': {'file_path': 'example.py', 'old_string': 'value = 1\n', 'new_string': 'value = 2\n'}}], 5, 'm2'),
                record('user', [{'type': 'tool_result', 'tool_use_id': 'edit', 'content': 'Updated example.py'}], 6),
                record('user', [media('image', 'image/png', png()), media('document', 'application/pdf', pdf()), media('audio', 'audio/wav', wav.getvalue()), media('document', 'image/svg+xml', b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="90"><rect width="300" height="90" fill="#569"/><text x="20" y="50" fill="white">Retained SVG</text></svg>')], 7)]
        path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
        source = Source('fixture', 'claude', path, path.name, 'fixture', 'fixture', title='Renderer verification', project=str(root))
        registry = types.SimpleNamespace(sources={'fixture': source}, _refresh_lock=threading.Lock(), source=lambda key: source,
                                         sessions=lambda *args: {'sessions': [source.public()], 'total': 1, 'next_cursor': None})
        state = object.__new__(ViewerState)
        state.registry, state.cache, state.config = registry, IndexCache(), {'excluded_workspaces': []}
        state.lock, state.token = threading.RLock(), 'synthetic-preview'
        state.source_status = lambda source: {'live_delta': False}
        build = pathlib.Path(__file__).resolve().parents[1] / 'viewer/dist'
        import mimetypes
        state.assets = {'/' + p.relative_to(build).as_posix(): (mimetypes.guess_type(p.name)[0] or 'application/octet-stream', p.read_bytes()) for p in build.rglob('*') if p.is_file()}
        state.assets['/'] = state.assets['/index.html']
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _handler(state))
        state.authority = f'127.0.0.1:{server.server_port}'
        print(f'http://{state.authority}/?token={state.token}', flush=True)
        try: server.serve_forever()
        except KeyboardInterrupt: pass
        finally: server.server_close()


if __name__ == '__main__':
    main()
