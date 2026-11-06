from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import time

_LOG_PATH = os.environ.get('L1_FAKE_LOG')


def _record(event):
    if not _LOG_PATH:
        return
    with open(_LOG_PATH, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + '\n')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path.rstrip('/') in ('/v1', '/v1/models'):
            body = b'{"object":"list","data":[{"id":"l1-fake-model","object":"model"}]}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode('utf-8'))
        except Exception:
            self.send_error(400)
            return
        messages = payload.get('messages') or []
        users = [m.get('content', '') for m in messages if m.get('role') == 'user']
        last = users[-1] if users else ''
        _record({'path': self.path, 'stream': payload.get('stream'), 'last_user': last})

        if '__ERROR__' in last:
            body = json.dumps({'error': {'message': 'l1 fake provider failure', 'type': 'server_error'}}).encode()
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        text = 'L1-FAKE[history=%d] 普通聊天流式验收；LAST=%s' % (len(users), last)
        chunks = [text[i:i + 22] for i in range(0, len(text), 22)] or ['']
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()
        for index, chunk in enumerate(chunks):
            data = {
                'id': 'l1-fake-%d' % index,
                'object': 'chat.completion.chunk',
                'created': int(time.time()),
                'model': 'l1-fake-model',
                'choices': [{'index': 0, 'delta': {'content': chunk}, 'finish_reason': None}],
            }
            self.wfile.write(('data: ' + json.dumps(data, ensure_ascii=False) + '\n\n').encode('utf-8'))
            self.wfile.flush()
            time.sleep(0.45)
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', 9110), Handler).serve_forever()
