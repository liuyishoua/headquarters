import tempfile
import unittest
from pathlib import Path
from headquarters.app import create_app
from headquarters.autonomy import AutonomyStore


class AutonomyApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = AutonomyStore(self.path)
        self.app = create_app(self.path, {}, auth=False, autonomy=self.store)
        self.client = self.app.test_client()
        self.headers = {'Origin': 'http://localhost', 'X-App-Request': '1'}

    def tearDown(self):
        self.temp.cleanup()

    def goal(self):
        response = self.client.post('/api/autonomy', headers=self.headers, json={
            'title': '独立目标', 'objective': '分析给定资料', 'completion_criteria': ['有证据的分析'],
            'source_material': '用户提供资料', 'constraints': '不发送任何外部消息'})
        self.assertEqual(response.status_code, 201)
        return response.json

    def test_goal_persistence_feedback_and_global_pause(self):
        goal = self.goal()
        self.assertEqual(goal['constraints'], '不发送任何外部消息')
        result = self.client.post('/api/autonomy/' + goal['id'] + '/feedback', headers=self.headers,
                                  json={'note': '新资料表明原假设不成立'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['observations'][-1]['kind'], 'user_feedback')
        self.assertEqual(self.client.post('/api/pause-all', headers=self.headers, json={}).json['paused'], 1)
        reopened = AutonomyStore(self.path)
        self.assertEqual(reopened.get(goal['id'])['status'], 'paused')

    def test_browser_cannot_grant_unknown_account_or_forge_completion(self):
        goal = self.goal()
        result = self.client.put('/api/autonomy/' + goal['id'] + '/grants', headers=self.headers,
                                json={'grants': [{'resource_id': 'unknown', 'operations': ['dm.reply']}]})
        self.assertEqual(result.status_code, 400)
        result = self.client.post('/api/autonomy/' + goal['id'] + '/feedback', headers=self.headers,
                                 json={'note': '已成交', 'kind': 'human_confirmation'})
        self.assertEqual(result.json['observations'][-1]['kind'], 'user_feedback')
        self.assertNotEqual(result.json['status'], 'completed')

    def test_cross_origin_and_artifact_paths_rejected(self):
        self.assertEqual(self.client.post('/api/autonomy', json={}).status_code, 403)
        goal = self.goal()
        self.assertEqual(self.client.get('/api/autonomy/' + goal['id'] + '/artifacts/session-key').status_code, 404)

    def test_final_confirmation_requires_owner_and_same_goal_evidence(self):
        goal = self.goal()
        wrong = self.store.observe(goal['id'], 'user_feedback', {'note': '模型称已成交'})
        result = self.client.post('/api/autonomy/' + goal['id'] + '/confirm', headers=self.headers,
                                 json={'evidence_ids': [wrong], 'note': '没有核实'})
        self.assertEqual(result.status_code, 400)
        evidence = self.store.observe(goal['id'], 'resource_read', {'source': 'synthetic_authenticated_observation'})
        result = self.client.post('/api/autonomy/' + goal['id'] + '/confirm', headers=self.headers,
                                 json={'evidence_ids': [evidence], 'note': '合成验收：所有交付条件已经核实'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
