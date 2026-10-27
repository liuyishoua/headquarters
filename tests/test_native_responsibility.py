"""Visible responsibility must never create a second native execution lane."""
import tempfile
import unittest
from headquarters.autonomy import AutonomyStore, MulticaAdapter, UncertainWrite

class Native:
    def __init__(self):self.calls=[];self.disabled=set();self.records={};self.uncertain=False
    def disable_child_wakeup(self,issue):self.calls.append(('disable',issue));self.disabled.add(issue)
    def create(self,kind,data):
        self.calls.append((kind,dict(data)))
        if kind=='project':return {'id':'project'}
        if kind=='issue':
            assert 'assignee_id' not in data
            issue='issue-'+str(len(self.records));self.records[issue]=dict(data);return {'id':issue}
        if kind.startswith('update_issue:'):
            issue=kind.split(':',1)[1];assert data['suppress_run'] is True
            if data.get('assignee_id'):assert issue in self.disabled
            self.records[issue].update(data)
            if self.uncertain:self.uncertain=False;raise UncertainWrite('accepted but response lost')
            return {'id':issue}
    def find(self,kind,marker):
        issue=kind.split(':',1)[1]
        return {'id':issue} if marker in self.records[issue].get('description','') else None

class ResponsibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.remote=Native()
        self.store=AutonomyStore(self.temp.name,multica=self.remote)
        self.goal=self.store.create({'title':'synthetic','objective':'read only','completion_criteria':['test'],'agent_id':'selected-live-agent'})
    def tearDown(self):self.temp.cleanup()
    def test_creation_assigns_selected_owner_after_disabling_parent_wakeup_without_dispatch(self):
        project,main=self.store.link_project(self.goal)
        self.assertEqual(self.remote.records[main]['assignee_id'],'selected-live-agent')
        self.assertTrue(self.remote.records[main]['suppress_run'])
        count=sum(k=='issue' for k,_ in self.remote.calls)
        self.store.link_project(self.goal)
        self.assertEqual(sum(k=='issue' for k,_ in self.remote.calls),count)
        self.store.remote_record(self.goal['id'],'stage-complete','update_issue:'+main,{'status':'done','description':'result'})
        self.assertTrue(self.remote.records[main]['suppress_run'])
    def test_uncertain_assignment_reconciles_without_creating_or_dispatching_again(self):
        self.remote.uncertain=True
        with self.assertRaises(UncertainWrite):self.store.link_project(self.goal)
        before=len([k for k,_ in self.remote.calls if k.startswith('update_issue:')])
        _,main=self.store.link_project(self.goal)
        self.assertEqual(len([k for k,_ in self.remote.calls if k.startswith('update_issue:')]),before)
        self.assertEqual(self.remote.records[main]['assignee_id'],'selected-live-agent')
    def test_adapter_child_rule_disable_uses_existing_nonexecuting_api(self):
        adapter=object.__new__(MulticaAdapter);calls=[]
        adapter.request=lambda *a:calls.append(a) or {}
        adapter.disable_child_wakeup('synthetic')
        self.assertEqual(calls,[('PUT','/api/issues/synthetic/system-wakeups/child_done',{'enabled':False,'instruction':''})])
