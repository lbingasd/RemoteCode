#!/usr/bin/env python3
"""Stdio ACP proxy. Remote approvals never create a second Codex session."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from qq_bot import local_call


class Proxy:
    def __init__(self, root, command, source=None, sink=None):
        self.root, self.command = root, command
        self.source = source or sys.stdin.buffer
        self.sink = sink or sys.stdout.buffer
        self.connection = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.child_lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.pending = {}
        self.stopped = threading.Event()
        self.child = None

    def api(self, route, body=None):
        return local_call(self.root, route, body, timeout=25 if route == '/permission' else 3)

    def emit(self, stream, lock, value):
        data = (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        with lock:
            stream.write(data)
            stream.flush()

    def agent(self, value):
        self.emit(self.child.stdin, self.child_lock, value)

    def client(self, value):
        self.emit(self.sink, self.output_lock, value)

    def cancel(self, session=None):
        with self.lock:
            for item in self.pending.values():
                if session is None or item['session'] == session:
                    item['cancelled'] = True
        try:
            self.api('/cancel', {'connection': self.connection, 'session': session})
        except (OSError, ValueError):
            pass

    def permission(self, message, state):
        key = json.dumps(message['id'])
        # JSON-RPC IDs may be reused after a request completes. Each invocation
        # needs a fresh identity so an earlier approval cannot authorize it.
        request_key = uuid.uuid4().hex
        remote = False
        try:
            result = self.api('/permission', {'connection': self.connection, 'request_key': request_key,
                                             'params': message['params']})
            remote = result.get('route') == 'qq'
            if not remote:
                with self.lock:
                    if not state['cancelled'] and not self.stopped.is_set():
                        self.pending.pop(key, None)
                        self.client(message)
                        return
                return
            deadline = time.monotonic() + 300
            while not self.stopped.is_set() and time.monotonic() < deadline:
                with self.lock:
                    if state['cancelled']:
                        break
                polled = self.api('/poll', {'id': result['id'], 'connection': self.connection})
                if polled.get('result') is not None:
                    # Recheck cancellation and write under the same lock as session/cancel.
                    with self.lock:
                        if not state['cancelled'] and not self.stopped.is_set():
                            self.agent({'jsonrpc': '2.0', 'id': message['id'], 'result': polled['result']})
                            self.pending.pop(key, None)
                            return
                    break
                self.stopped.wait(0.5)
        except (OSError, ValueError):
            if not remote:
                # Network timeout may have registered a request. Invalidate it first.
                # If invalidation fails, the lease expires in 15 s and no reply is read.
                try:
                    self.api('/cancel', {'connection': self.connection, 'session': state['session']})
                except (OSError, ValueError):
                    pass
                with self.lock:
                    if not state['cancelled'] and not self.stopped.is_set():
                        self.pending.pop(key, None)
                        self.client(message)
                        return
        finally:
            with self.lock:
                outstanding = self.pending.pop(key, None)
                if outstanding and not self.stopped.is_set():
                    self.agent({'jsonrpc': '2.0', 'id': message['id'],
                                'result': {'outcome': {'outcome': 'cancelled'}}})

    def incoming(self):
        try:
            for line in self.source:
                message = json.loads(line)
                if message.get('method') == 'session/cancel':
                    # Cancel locally before forwarding, without blocking the cancellation on HTTP.
                    session = message.get('params', {}).get('sessionId')
                    with self.lock:
                        for p in self.pending.values():
                            if p['session'] == session:
                                p['cancelled'] = True
                        self.agent(message)
                    threading.Thread(target=self.cancel, args=(session,), daemon=True).start()
                else:
                    self.agent(message)
        except (ValueError, OSError):
            pass
        finally:
            self.stopped.set()
            self.cancel()
            try:
                self.child.stdin.close()
                self.child.terminate()
            except OSError:
                pass

    def run(self):
        # Adapter stderr may contain model/tool output. It stays on the original IDE stderr.
        self.child = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None)
        threading.Thread(target=self.incoming, daemon=True).start()
        try:
            for line in self.child.stdout:
                message = json.loads(line)
                if message.get('method') == 'session/request_permission' and 'id' in message:
                    key = json.dumps(message['id'])
                    state = {'session': message.get('params', {}).get('sessionId'), 'cancelled': False}
                    with self.lock:
                        self.pending[key] = state
                    threading.Thread(target=self.permission, args=(message, state), daemon=True).start()
                else:
                    self.client(message)
        finally:
            self.stopped.set()
            self.cancel()
            if self.child.poll() is None:
                self.child.terminate()
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait()
        return self.child.returncode


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True, type=Path)
    p.add_argument('command', nargs=argparse.REMAINDER)
    args = p.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        p.error('missing adapter command')
    return Proxy(args.root.resolve(), command).run()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError):
        print('RemoteCode ACP proxy failed; reconnect the agent.', file=sys.stderr)
        sys.exit(1)
