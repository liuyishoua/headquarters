import tempfile
import unittest
from headquarters.core import Headquarters
from headquarters.capabilities import CapabilityDirectory


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hq = Headquarters(self.temp.name, {})
        self.calls = []
        def api(project, path, account_id=None):
            self.calls.append((path, account_id))
            if path == '/api/accounts':
                data = {'items': [{'id': 'a', 'name': '甲'}, {'id': 'b', 'name': '乙'}]}
            elif path == '/api/products':
                data = {'items': [{'id': account_id + '-product', 'title': '商品'}]}
            elif path == '/api/conversations':
                data = {'items': [{'id': account_id + '-conversation'}]}
            else:
                data = {'items': [{'content': '授权消息'}]}
            return {'status': 200, 'data': data}
        self.directory = CapabilityDirectory(self.hq, api)
        self.a, self.b = self.directory()['resources']

    def tearDown(self):
        self.temp.cleanup()

    def test_selected_account_not_default_and_scope(self):
        goal = {'grants': [{'resource_id': self.b['id'], 'operations': ['product.read'],
                           'scope': {'resource_ids': ['b-product']}}]}
        result = self.directory.execute(goal, {'arguments': {'resource_id': self.b['id'], 'operation': 'product.read'}})
        self.assertEqual(result['items'][0]['id'], 'b-product')
        self.assertIn(('/api/products', 'b'), self.calls)
        with self.assertRaises(ValueError):
            self.directory.execute(goal, {'arguments': {'resource_id': self.a['id'], 'operation': 'product.read'}})

    def test_message_scope_and_account_ownership(self):
        goal = {'grants': [{'resource_id': self.a['id'], 'operations': ['message.read'],
                           'scope': {'conversation_ids': ['a-conversation', 'b-conversation']}}]}
        self.directory.execute(goal, {'arguments': {'resource_id': self.a['id'], 'operation': 'message.read', 'conversation_id': 'a-conversation'}})
        self.assertIn(('/api/conversations/a-conversation/messages', 'a'), self.calls)
        with self.assertRaises(ValueError):
            self.directory.execute(goal, {'arguments': {'resource_id': self.a['id'], 'operation': 'message.read', 'conversation_id': 'b-conversation'}})
        with self.assertRaises(ValueError):
            self.directory.execute(goal, {'arguments': {'resource_id': self.a['id'], 'operation': 'message.read', 'conversation_id': '../secrets'}})

    def test_unimplemented_operations_unavailable(self):
        self.assertFalse(next(o for o in self.a['operations'] if o['id'] == 'product.modify')['available'])
        goal = {'grants': [{'resource_id': self.a['id'], 'operations': ['dm.initiate']}]}
        with self.assertRaises(ValueError):
            self.directory.execute(goal, {'arguments': {'resource_id': self.a['id'], 'operation': 'dm.initiate'}})

    def test_xhs_uses_explicit_owned_sdk_account_and_marks_coverage(self):
        self.directory.xhs_owner_id = 7
        self.directory.expires = 0
        calls = []
        def xhs(operation, account=None):
            calls.append((operation, account))
            if operation == 'catalog':
                return {'items': [{'id': 'owned-pc', 'name': '合成人物', 'status': 'active'}]}
            return {'items': [{'note_id': 'published-note'}], 'coverage': 'recent_first_page'}
        self.directory._xhs = xhs
        resource = next(r for r in self.directory()['resources'] if r['source_id'] == 'xhs')
        goal = {'grants': [{'resource_id': resource['id'], 'operations': ['content.read']}]}
        result = self.directory.execute(goal, {'arguments': {'resource_id': resource['id'], 'operation': 'content.read'}})
        self.assertEqual(result['source'], 'authenticated_platform_adapter')
        self.assertEqual(result['data']['coverage'], 'recent_first_page')
        self.assertIn(('content.read', 'owned-pc'), calls)
        with self.assertRaises(ValueError):
            self.directory.execute(goal, {'arguments': {'resource_id': resource['id'], 'operation': 'profile.read'}})


if __name__ == '__main__':
    unittest.main()

class MachineAvailabilityTests(unittest.TestCase):
    def test_writes_require_machine_feature_online_exact_account_connector(self):
        import copy
        from pathlib import Path
        class Client:
            def __init__(self):
                self.feature = True
                self.connectors = [dict(source='synthetic',account='a',online=True,capabilities=['dm.reply']),
                                   dict(source='synthetic',account='b',online=False,capabilities=['dm.reply'])]
            def catalog(self):
                return dict(direct_goal_actions=self.feature,connectors=copy.deepcopy(self.connectors))
        with tempfile.TemporaryDirectory() as tmp:
            client = Client()
            class HQ:
                directory = Path(tmp)
                clients = {'test':client}
                def overview(self):
                    return dict(channels=[dict(id='test',snapshot=client.catalog())],accounts=[])
            def unavailable(*args,**kwargs):
                raise ValueError('synthetic only')
            directory = CapabilityDirectory(HQ(),unavailable)
            rows = directory()['resources']
            available = lambda row: next(op['available'] for op in row['operations'] if op['id']=='dm.reply')
            self.assertTrue(available(next(row for row in rows if row['account_id']=='a')))
            self.assertFalse(available(next(row for row in rows if row['account_id']=='b')))
            client.feature=False
            directory.expires=0
            self.assertFalse(any(available(row) for row in directory()['resources']))
