"""Goal-bound machine queue adapter. No platform cookies or source tokens.

Remote intents are idempotent and delivery is proven only by connector receipts.
The independent local ledger preserves deployment revisions across restarts.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import quote

WRITES = {'dm.reply', 'comment.reply', 'content.publish'}


class ChannelGoalExecutor:
    def __init__(self, directory, clients, directory_provider):
        self.clients, self.directory_provider = clients, directory_provider
        self.path = Path(directory) / 'channel-executor.sqlite'
        self.lock = threading.RLock()
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS deployments(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,resource_id TEXT NOT NULL,channel_id TEXT NOT NULL,revision INTEGER NOT NULL,fingerprint TEXT NOT NULL,payload TEXT NOT NULL)')

    def db(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def selected(self, goal, action):
        args = action.get('arguments', {})
        key, operation = args.get('resource_id'), args.get('operation')
        grant = next((g for g in goal.get('grants', []) if g['resource_id'] == key and operation in g['operations']), None)
        resource = next((r for r in self.directory_provider()['resources'] if r['id'] == key), None)
        if goal.get('status') != 'active' or not grant or not resource or operation not in WRITES:
            raise ValueError('Goal does not authorize this account operation')
        if float(grant.get('expires_at', 0)) <= time.time() or time.time() >= goal.get('deadline', float('inf')):
            raise ValueError('Goal execution authority expired')
        client = self.clients.get(resource['channel_id'])
        if not client:
            raise ValueError('Account has no authenticated command connection')
        catalog = client.catalog()
        if not catalog.get('direct_goal_actions'):
            raise ValueError('Channel does not support direct goal actions')
        connector = next((c for c in catalog.get('connectors', []) if c['source'] == resource['source_id'] and c['account'] == resource['account_id'] and c.get('online') and operation in c['capabilities']), None)
        if not connector:
            raise ValueError('Exact account operation executor is offline')
        target = args.get('target', '')
        prefix = {'dm.reply': 'dm:', 'comment.reply': 'comment:', 'content.publish': 'content:'}[operation]
        ids = grant.get('scope', {}).get('conversation_ids' if operation == 'dm.reply' else 'resource_ids', [])
        targets = [prefix + value for value in ids]
        if target not in targets:
            raise ValueError('Action target is outside explicit account scope')
        grant = dict(grant, operations=[operation])
        return resource, grant, client

    def deploy(self, goal, resource, grant, client, enabled=True):
        deployment_id = 'autonomy-' + hashlib.sha256((goal['id'] + ':' + resource['id'] + ':' + grant['operations'][0]).encode()).hexdigest()[:40]
        scope = grant.get('scope', {})
        operation = grant['operations'][0]
        prefix = {'dm.reply': 'dm:', 'comment.reply': 'comment:', 'content.publish': 'content:'}[operation]
        targets = [prefix + x for x in scope.get('conversation_ids' if operation == 'dm.reply' else 'resource_ids', [])]
        allowed = sorted(set(grant['operations']) & WRITES)
        payload = dict(mission_id=goal['id'], assignment_id=resource['id'], lease_seconds=30,
            context=dict(execution_grant=dict(source_id=resource['source_id'], account_id=resource['account_id'], operations=allowed, targets=targets, expires_at=min(grant['expires_at'], goal['deadline']))),
            goal=dict(source_id=resource['source_id'], account_id=resource['account_id'],
                name=goal['title'][:80], objective=goal['objective'][:2000], agent_id=goal['agent_id'],
                enabled=enabled, mode='automatic', event_types=['content.requested'], allowed_actions=allowed,
                hour_start=0, hour_end=24, max_followups=0))
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.lock, self.db() as db:
            row = db.execute('SELECT * FROM deployments WHERE id=?', (deployment_id,)).fetchone()
            revision = row['revision'] if row and row['fingerprint'] == fingerprint else (row['revision'] + 1 if row else 1)
            payload['revision'] = revision
            # Save desired intent before remote write; retry same revision/body.
            db.execute('INSERT INTO deployments VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,fingerprint=excluded.fingerprint,payload=excluded.payload',
                (deployment_id, goal['id'], resource['id'], resource['channel_id'], revision, fingerprint, json.dumps(payload)))
            db.commit()
            client.deploy(deployment_id, payload)
        return deployment_id, revision

    def execute(self, goal, action):
        resource, grant, client = self.selected(goal, action)
        deployment_id, revision = self.deploy(goal, resource, grant, client)
        args = action['arguments']
        request_id = action.get('id') or args.get('request_id')
        if not request_id:
            raise ValueError('Durable action identity is required')
        body = dict(request_id=request_id, revision=revision, source_id=resource['source_id'], account_id=resource['account_id'],
                    operation=args['operation'], target=args['target'])
        if args['operation'] == 'content.publish':
            body['title'] = args.get('title') or args.get('text', '').split('\n', 1)[0][:100]
        body.update({k: args[k] for k in ('text', 'title', 'images') if k in args})
        row = client.request('POST', '/control/v1/deployments/' + deployment_id + '/actions', body)
        return self.result(row, resource['channel_id'], deployment_id, request_id)

    @staticmethod
    def result(row, channel_id, deployment_id, request_id):
        state = row['state']
        result = dict(pending=state in ('ready', 'review', 'claimed'), remote_action_id=row['id'],
                      channel_id=channel_id, deployment_id=deployment_id, request_id=request_id,
                      source_id=row['source'], account_id=row['account'], target=row['target'], operation=row['type'])
        if not result['pending']:
            result['status'] = 'executed' if state == 'succeeded' and (row.get('receipt') or {}).get('external_id') else 'uncertain' if state == 'uncertain' else 'failed'
            result['evidence'] = dict(source='authenticated_channel_receipt', receipt=row.get('receipt'), queue_state=state)
        return result

    def poll(self, goal, action):
        result = action['result']
        client = self.clients[result['channel_id']]
        try:
            resource, grant, _ = self.selected(goal, action)
        except ValueError:
            self.cancel(goal, action)
        else:
            self.deploy(goal, resource, grant, client)
        row = client.request('GET', '/control/v1/deployments/' + result['deployment_id'] + '/actions/' + quote(result['request_id'], safe=''))
        if any(row[k] != result[v] for k, v in [('id', 'remote_action_id'), ('source', 'source_id'), ('account', 'account_id'), ('target', 'target'), ('type', 'operation')]):
            raise ValueError('Remote receipt identity mismatch')
        return self.result(row, result['channel_id'], result['deployment_id'], result['request_id'])

    def cancel(self, goal, action):
        result = action.get('result', {})
        if not result.get('deployment_id'):
            return
        return self.clients[result['channel_id']].request('POST', '/control/v1/deployments/' + result['deployment_id'] + '/actions/' + quote(result['request_id'], safe='') + '/cancel', {})

    def sync_goal(self, goal):
        """Immediately disable removed grants, pause/stop and expired authority."""
        with self.lock, self.db() as db:
            rows = db.execute('SELECT * FROM deployments WHERE run_id=?', (goal['id'],)).fetchall()
            for row in rows:
                grant = next((g for g in goal.get('grants', []) if g['resource_id'] == row['resource_id']), None)
                valid = goal['status'] == 'active' and grant and grant.get('expires_at', 0) > time.time() and goal.get('deadline', 0) > time.time()
                payload = json.loads(row['payload'])
                old = payload['context']['execution_grant']
                if valid:
                    scope = grant.get('scope', {})
                    operation = old['operations'][0]
                    prefix = {'dm.reply': 'dm:', 'comment.reply': 'comment:', 'content.publish': 'content:'}[operation]
                    targets = [prefix + x for x in scope.get('conversation_ids' if operation == 'dm.reply' else 'resource_ids', [])]
                    valid = set(old['operations']) <= set(grant['operations']) and set(old['targets']) <= set(targets)
                if valid:
                    continue
                payload['goal']['enabled'] = False
                payload['revision'] = row['revision'] if row['fingerprint'] == 'disabled' else row['revision'] + 1
                # Changed fingerprint forces a new revision on later resume.
                db.execute('UPDATE deployments SET revision=?,fingerprint=?,payload=? WHERE id=?',
                    (payload['revision'], 'disabled', json.dumps(payload), row['id']))
                db.commit()
                self.clients[row['channel_id']].deploy(row['id'], payload)
