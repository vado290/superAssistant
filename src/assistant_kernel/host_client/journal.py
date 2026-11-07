"""Local execution journal: never replay an operation after an uncertain interruption."""
import json
import sqlite3
from pathlib import Path


class OperationJournal:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, body TEXT)')

    def connect(self):
        db = sqlite3.connect(self.path)
        db.execute('PRAGMA synchronous=FULL')
        return db

    @staticmethod
    def fingerprint(op):
        # Tokens are intentionally not stored in the journal.
        return json.dumps({key: op.get(key) for key in
                          ('project_id', 'run_id', 'action', 'arguments', 'capability_revision', 'caller_identity')},
                          sort_keys=True, separators=(',', ':'))

    def begin(self, op):
        identity = self.fingerprint(op)
        with self.connect() as db:
            row = db.execute('SELECT fingerprint, body FROM operations WHERE id=?', (op['operation_id'],)).fetchone()
            if row:
                if row[0] != identity:
                    raise ValueError('operation identity changed')
                if row[1]:
                    return json.loads(row[1])
                return {'state': 'failed', 'result': {'error': {'code': 'EXECUTION_UNKNOWN',
                        'message': 'Execution was interrupted; inspect the external resource before retrying.'}}}
            db.execute('INSERT INTO operations(id, fingerprint) VALUES (?, ?)', (op['operation_id'], identity))
        return None

    def finish(self, op, body):
        stored = {key: body[key] for key in ('state', 'result')}
        with self.connect() as db:
            count = db.execute('UPDATE operations SET body=? WHERE id=? AND fingerprint=?',
                               (json.dumps(stored), op['operation_id'], self.fingerprint(op))).rowcount
            if count != 1:
                raise ValueError('operation identity changed')
