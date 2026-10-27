import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'channel'))
from channel_service.automation import Automation
from channel_service.command_actions import queue, read
from channel_service.demo import AGENT_ID
from channel_service.store import Store
from headquarters.channel_executor import ChannelGoalExecutor


class FakeMachine:
    def __init__(self, automation):
        self.automation = automation
        self.replies = 0

    def catalog(self):
        return dict(direct_goal_actions=True, connectors=self.automation.connectors())

    def deploy(self, goal_id, payload):
        authority = {k: payload[k] for k in ('revision', 'mission_id', 'assignment_id', 'context', 'lease_seconds')}
        authority['owner'] = 'hq'
        return self.automation.save_goal(payload['goal'], goal_id, control=authority)

    def request(self, method, path, data=None):
        parts = path.split('/')
        goal_id = parts[4]
        if method == 'POST' and parts[-1] == 'actions':
            self.replies += 1
            return queue(self.automation, 'hq', goal_id, data)
        if method == 'GET':
            return read(self.automation, 'hq', goal_id, parts[-1])
        return read(self.automation, 'hq', goal_id, parts[-2], cancel=True)


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auto = Automation(Store(self.tmp.name))
        self.auto.store.source('synthetic')
        self.auto.heartbeat('synthetic', dict(account_id='fake-a', executor_id='fake', capabilities=['dm.reply', 'content.publish']))
        self.machine = FakeMachine(self.auto)
        self.resource = dict(id='resource-a', source_id='synthetic', account_id='fake-a', channel_id='channel-a')
        self.provider = lambda: {'resources': [self.resource]}
        self.goal = dict(id='goal-a', title='synthetic', objective='synthetic', agent_id=AGENT_ID, status='active', deadline=time.time()+100,
            grants=[dict(resource_id='resource-a', operations=['dm.reply', 'content.publish'], scope=dict(conversation_ids=['c1'], resource_ids=['r1']), expires_at=time.time()+100)])
        self.action = dict(id='action-a', kind='execute_resource', arguments=dict(resource_id='resource-a', operation='dm.reply', target='dm:c1', text='synthetic'))
        self.executor = ChannelGoalExecutor(self.tmp.name, {'channel-a': self.machine}, self.provider)
        self.auto.ingest('synthetic', dict(account_id='fake-a', event_id='in-1', event_type='dm.received', conversation_id='c1', content={'text':'synthetic'}))

    def test_actual_queue_and_authenticated_receipt_poll(self):
        pending = self.executor.execute(self.goal, self.action)
        self.assertTrue(pending['pending'])
        self.action['result'] = pending
        restarted = ChannelGoalExecutor(self.tmp.name, {'channel-a': self.machine}, self.provider)
        self.assertTrue(restarted.poll(self.goal, self.action)['pending'])
        claimed = self.auto.claim('synthetic', dict(account_id='fake-a', executor_id='fake'))['action']
        self.auto.receipt('synthetic', claimed['id'], dict(account_id='fake-a',claim_token=claimed['claim_token'],status='succeeded',external_id='synthetic-platform-id'))
        result = restarted.poll(self.goal, self.action)
        self.assertEqual(result['status'], 'executed')
        self.assertFalse(result['pending'])
        self.assertEqual(self.machine.replies, 1)
        self.assertEqual(result['evidence']['receipt']['external_id'], 'synthetic-platform-id')

    def test_cross_scope_expiry_and_pause(self):
        for change in ({'target':'dm:other'}, {'resource_id':'other'}):
            action = copy.deepcopy(self.action)
            action['arguments'].update(change)
            with self.assertRaises(ValueError):
                self.executor.execute(self.goal, action)
        self.action['result'] = self.executor.execute(self.goal, self.action)
        self.goal['status'] = 'paused'
        self.executor.sync_goal(self.goal)
        self.assertIsNone(self.auto.claim('synthetic',dict(account_id='fake-a',executor_id='fake'))['action'])
        result = self.executor.poll(self.goal,self.action)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['evidence']['queue_state'],'cancelled')

    def test_scope_revocation_disables_remote_queue(self):
        self.action['result'] = self.executor.execute(self.goal,self.action)
        self.goal['grants'][0]['scope']['conversation_ids'] = []
        self.executor.sync_goal(self.goal)
        self.assertIsNone(self.auto.claim('synthetic',dict(account_id='fake-a',executor_id='fake'))['action'])
        self.assertEqual(self.executor.poll(self.goal,self.action)['status'],'failed')

    def test_revoke_one_operation_preserves_other_queue(self):
        self.action['result'] = self.executor.execute(self.goal,self.action)
        publish = copy.deepcopy(self.action)
        publish['id'] = 'publication'
        publish['arguments'].update(operation='content.publish',target='content:r1',text='Synthetic publication')
        publish['result'] = self.executor.execute(self.goal,publish)
        self.goal['grants'][0]['operations'] = ['content.publish']
        self.executor.sync_goal(self.goal)
        dm = self.executor.poll(self.goal,self.action)
        self.assertEqual(dm['evidence']['queue_state'],'cancelled')
        self.assertTrue(self.executor.poll(self.goal,publish)['pending'])
        claimed = self.auto.claim('synthetic',dict(account_id='fake-a',executor_id='fake'))['action']
        self.assertEqual(claimed['type'],'content.publish')

    def test_publish_uses_real_queue_not_inbound_simulation(self):
        self.action['arguments'].update(operation='content.publish',target='content:r1',text='Synthetic title\nSynthetic body')
        self.action['result']=self.executor.execute(self.goal,self.action)
        claimed=self.auto.claim('synthetic',dict(account_id='fake-a',executor_id='fake'))['action']
        self.assertEqual(claimed['payload']['title'],'Synthetic title')
        with self.auto.store.db() as db:
            payload=json.loads(db.execute("SELECT payload FROM business_events WHERE state='command_requested'").fetchone()[0])
        self.assertEqual(payload['event_type'],'command.requested')
        self.assertEqual(payload['direction'],'system')
