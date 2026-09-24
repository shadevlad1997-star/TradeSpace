"""Local merchant receiver for recovery smoke tests; never a production service."""
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg

from scripts.recovery_run import local_environment

ENV = local_environment()

COUNTS = {}
LOCK = threading.Lock()
LOG = Path('logs/webhook-receiver.jsonl')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200 if self.path == '/health' else 404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        if length > 2_000_000:
            self.send_response(413)
            self.end_headers()
            return
        body = self.rfile.read(length)
        key_id = self.headers.get('X-TradeSpace-Key-ID', '')
        with psycopg.connect(ENV['SYNC_DATABASE_URL'].replace('+psycopg', '')) as db:
            row = db.execute('SELECT encrypted_secret FROM merchant_webhook_signing_keys WHERE key_id=%s', (key_id,)).fetchone()
        valid = False
        if row:
            encrypted = row[0]
            # Match the existing app encryption envelope without logging secrets.
            from app.core.security import decrypt_secret
            secret = decrypt_secret(encrypted)
            message = self.headers.get('X-TradeSpace-Timestamp', '').encode() + b'.' + body
            expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
            valid = hmac.compare_digest(expected, self.headers.get('X-TradeSpace-Signature', ''))
        event_id = self.headers.get('X-TradeSpace-Event-ID', '')
        with LOCK:
            COUNTS[event_id] = COUNTS.get(event_id, 0) + 1
            status = 204 if valid else 401
            if valid and self.path == '/retry-once' and COUNTS[event_id] == 1:
                status = 500
            elif valid and self.path == '/reject':
                status = 400
            with LOG.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'event_id': event_id, 'signature_valid': valid,
                    'response_status': status, 'event': json.loads(body).get('event'),
                    'contains_api_key_header': bool(self.headers.get('X-API-Key'))}) + '\n')
        self.send_response(status)
        self.send_header('Content-Length', '0')
        self.end_headers()


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', 18081), Handler).serve_forever()
