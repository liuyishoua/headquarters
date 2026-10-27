import json
from pathlib import Path
import tempfile
import unittest
from headquarters.autonomy import AutonomyStore, AutonomyError, UncertainWrite, PlanningPending, RetryableError


class Planner:
    def __init__(self, function):
        self.function = function
        self.calls = 0
    def plan(self, context):
        self.calls += 1
        return self.function(context)


def decision(kind, arguments):
    return dict(kind=kind, arguments=arguments, summary='Next step', rationale='Based on observed results')


class AutonomyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.clock = lambda: self.now
    def tearDown(self):
        self.temp.cleanup()
    def store(self, **kwargs):
        return AutonomyStore(self.temp.name, clock=self.clock, **kwargs)
    def goal(self, **kwargs):
        return dict(title='Research goal', objective='Prepare an evidence-backed report',
                    completion_criteria=['Report ready'], **kwargs)
    def advance(self, store, run):
        self.now += 11
        return store.tick(run['id'])

    def test_two_domains_adjust_to_observed_fixture_then_verified_artifact(self):
        for domain, fixture in [('market','Customers prefer flexibility, not lowest price'),
                                ('research','Trial found longer runtime but fewer errors')]:
            def plan(c):
                if not c['observations']:
                    return decision('read_fixture', {'name':domain})
                artifacts = [x for x in c['observations'] if x['kind']=='artifact']
                if not artifacts:
                    observed = c['observations'][0]
                    return decision('write_artifact', {'name':domain+'.md',
                        'content':'Finding: '+observed['data']['content'], 'evidence_ids':[observed['id']]})
                return decision('complete', {'evidence_ids':[artifacts[0]['id']]})
            store = self.store(planner=Planner(plan), fixtures={domain:fixture})
            run = store.create(self.goal(delivery_type='local_artifact',artifact_checks={
                'Report ready':{'name':domain+'.md','contains':['Finding:',fixture]}}))
            store.tick(run['id'])
            self.advance(store,run)
            final = self.advance(store,run)
            self.assertEqual(final['status'],'completed')
            self.assertEqual(len(final['actions']),3)
            self.assertEqual(final['actions'][-1]['result']['verified_criteria'],['Report ready'])

    def test_input_request_blocks_and_feedback_resumes(self):
        store=self.store(planner=Planner(lambda c:decision('request_input',{'question':'发布地点是哪个城市？','missing_fields':['location']})))
        run=store.create(self.goal())
        result=store.tick(run['id'])
        self.assertEqual(result['status'],'blocked')
        self.assertTrue(result['actions'][-1]['result']['needs_input'])
        self.assertEqual(store.wake(run['id'],'user_feedback',{'location':'真实地点'})['status'],'active')

    def test_repeated_wait_without_feedback_stops(self):
        store=self.store(planner=Planner(lambda c:decision('wait',{'seconds':30})))
        run=store.create(self.goal())
        for _ in range(3):
            self.now+=31;result=store.tick(run['id'])
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['actions'][-1]['kind'],'request_input')

    def test_model_cannot_claim_business_completion(self):
        store=self.store(planner=Planner(lambda c:decision('complete',{'evidence_ids':[]})))
        run=store.create(self.goal())
        result=store.tick(run['id'])
        self.assertEqual(result['status'],'active')
        self.assertEqual(result['actions'][0]['status'],'failed')
        self.assertIn('independent',result['actions'][0]['result']['error'])

    def test_unavailable_capability_blocked(self):
        store=self.store(planner=Planner(lambda c:decision('dm.initiate',{})))
        run=store.create(self.goal())
        final=store.tick(run['id'])
        self.assertEqual(final['status'],'blocked')
        self.assertEqual(final['actions'],[])

    def test_missing_planner_does_not_fabricate_success(self):
        store=self.store(); run=store.create(self.goal())
        self.assertEqual(store.tick(run['id'])['status'],'blocked')

    def test_pause_during_model_call_prevents_action(self):
        store=self.store(fixtures={'one':'source'})
        def plan(c):
            store.control(c['id'],'pause')
            return decision('read_fixture',{'name':'one'})
        store.planner=Planner(plan)
        run=store.create(self.goal())
        final=store.tick(run['id'])
        self.assertEqual(final['status'],'paused')
        self.assertEqual(final['actions'],[])

    def test_duplicate_action_suppressed(self):
        store=self.store(fixtures={'one':'source'},planner=Planner(lambda c:decision('read_fixture',{'name':'one'})))
        run=store.create(self.goal());store.tick(run['id'])
        final=self.advance(store,run)
        self.assertEqual(final['status'],'blocked')
        self.assertEqual(len(final['actions']),1)

    def test_budget_and_deadline_are_terminal(self):
        store=self.store(planner=Planner(lambda c:decision('wait',{'seconds':30})))
        run=store.create(self.goal(limits={'max_actions':1}))
        store.tick(run['id']);self.now+=40
        self.assertEqual(store.tick(run['id'])['status'],'exhausted')
        run=store.create(self.goal(deadline=self.now+1));self.now+=2
        self.assertEqual(store.tick(run['id'])['status'],'expired')

    def test_restart_preserves_wait_and_lease(self):
        planner=Planner(lambda c:decision('wait',{'seconds':30}))
        store=self.store(planner=planner);run=store.create(self.goal());store.tick(run['id'])
        restored=self.store(planner=planner)
        self.assertEqual(restored.get(run['id'])['actions'][0]['status'],'completed')
        self.assertIsNone(restored.tick(run['id']))
        self.now+=31
        self.assertEqual(restored.tick(run['id'])['status'],'active')

    def test_lease_excludes_second_worker(self):
        store=self.store(planner=Planner(lambda c:decision('wait',{'seconds':30})))
        run=store.create(self.goal())
        with store.db() as db:
            db.execute('UPDATE runs SET lease_owner=?,lease_until=? WHERE id=?',('other',self.now+30,run['id']))
        self.assertIsNone(self.store().tick(run['id']))

    def test_interrupted_action_requires_reconciliation(self):
        store=self.store(planner=Planner(lambda c:decision('wait',{'seconds':30})))
        run=store.create(self.goal());store.tick(run['id'])
        with store.db() as db:
            db.execute("UPDATE actions SET status='running' WHERE run_id=?",(run['id'],))
            db.execute('UPDATE runs SET next_check=0 WHERE id=?',(run['id'],))
        self.assertEqual(self.store().tick(run['id'])['status'],'blocked')

    def test_remote_ambiguous_write_never_repeated(self):
        class Remote:
            calls=0
            found=None
            def create(self,k,d):
                self.calls+=1
                raise UncertainWrite('ambiguous')
            def find(self,k,m): return self.found
        remote=Remote(); store=self.store(multica=remote)
        run=store.create(self.goal())
        with self.assertRaises(UncertainWrite):store.remote_record(run['id'],'marker','issue',{'title':'test'})
        with self.assertRaises(UncertainWrite):store.remote_record(run['id'],'marker','issue',{'title':'test'})
        self.assertEqual(remote.calls,1)
        remote.found={'id':'confirmed'}
        self.assertEqual(store.remote_record(run['id'],'marker','issue',{'title':'test'}),'confirmed')
        self.assertEqual(remote.calls,1)

    def catalog(self):
        return {'resources':[
            {'id':'one','channel_id':'c','source_id':'xianyu','account_id':'1','operations':[{'id':'product.read','available':True}]},
            {'id':'two','channel_id':'c','source_id':'xianyu','account_id':'2','operations':[{'id':'conversation.read','available':True}]}]}

    def test_multi_account_grants_persist_and_revoke_independently(self):
        store=self.store(capability_provider=self.catalog)
        run=store.create(self.goal(grants=[{'resource_id':'one','operations':['product.read']},
                                        {'resource_id':'two','operations':['conversation.read']}]))
        self.assertEqual(len(self.store(capability_provider=self.catalog).get(run['id'])['grants']),2)
        changed=store.update_grants(run['id'],[{'resource_id':'two','operations':['conversation.read']}])
        self.assertEqual(changed['grants'][0]['account_id'],'2')
        self.assertEqual(len(changed['grants']),1)
        self.assertEqual(changed['revision'],1)

    def test_wrong_account_operation_rejected_before_executor(self):
        calls=[]
        store=self.store(capability_provider=self.catalog,executor=lambda r,a:calls.append(a),
            planner=Planner(lambda c:decision('read_resource',{'resource_id':'two','operation':'conversation.read'})))
        run=store.create(self.goal(grants=[{'resource_id':'one','operations':['product.read']}]))
        result=store.tick(run['id'])
        self.assertEqual(calls,[])
        self.assertEqual(result['actions'][0]['status'],'failed')

    def test_operation_disappearance_and_expiry_fail_closed(self):
        catalog=self.catalog()
        store=self.store(capability_provider=lambda:catalog,executor=lambda r,a:{'items':[]},
            planner=Planner(lambda c:decision('read_resource',{'resource_id':'one','operation':'product.read'})))
        run=store.create(self.goal(grants=[{'resource_id':'one','operations':['product.read'],'expires_at':self.now+1}]))
        self.now+=2
        result=store.tick(run['id'])
        self.assertEqual(result['actions'][0]['status'],'failed')
        with self.assertRaises(AutonomyError):store.create(self.goal(grants=[{'resource_id':'two','operations':['product.read']}]))

    def test_artifact_escape_and_cross_goal_evidence_rejected(self):
        store=self.store(fixtures={'one':'source'})
        with self.assertRaises(AutonomyError):store.validate(decision('write_artifact',{'name':'../escape.md','content':'x','evidence_ids':[]}))
        run=store.create(self.goal());other=store.create(self.goal())
        obs=store.observe(other['id'],'fixture',{'name':'one','content':'source'})
        with self.assertRaises(AutonomyError):store.execute(run,'action',decision('write_artifact',{'name':'safe.md','content':'x','evidence_ids':[obs]}))

    def write_catalog(self):
        return {'resources':[{'id':'one','channel_id':'c','source_id':'xianyu','account_id':'1',
            'operations':[{'id':'dm.reply','available':True}]}]}

    def test_external_pending_action_is_polled_not_resubmitted(self):
        calls=[]
        def executor(goal,action):
            calls.append(action)
            return {'pending':True,'remote_action_id':'remote'}
        polls=[]
        def poller(goal,action):
            polls.append(action)
            return {'pending':len(polls)==1,'status':'executed','evidence':{'receipt':'authenticated'}}
        planner=Planner(lambda c:decision('execute_resource',{
            'resource_id':'one','operation':'dm.reply','target':'dm:known','text':'Reply'}))
        store=self.store(capability_provider=self.write_catalog,executor=executor,poller=poller,planner=planner)
        run=store.create(self.goal(grants=[{'resource_id':'one','operations':['dm.reply'],'scope':{'conversation_ids':['known']}}]))
        first=store.tick(run['id'])
        self.assertEqual(first['actions'][0]['status'],'pending')
        self.advance(store,run);final=self.advance(store,run)
        self.assertEqual(final['actions'][0]['status'],'completed')
        self.assertEqual(len(calls),1)
        self.assertEqual(len(polls),2)
        self.assertEqual(planner.calls,1)
        self.assertEqual(calls[0]['arguments']['request_id'],calls[0]['id'])
        self.assertIn('executor_feedback', [x['kind'] for x in final['observations']])
        self.assertIn('external_receipt', [x['kind'] for x in final['observations']])

    def test_transient_planning_retry_survives_restart_and_is_bounded(self):
        def unavailable(context):
            raise RetryableError('Temporarily unavailable')
        store=self.store(planner=Planner(unavailable))
        run=store.create(self.goal())
        first=store.tick(run['id'])
        self.assertEqual(first['status'],'active')
        self.assertEqual(first['actions'],[])
        store=self.store(planner=Planner(unavailable))
        self.assertIsNone(store.tick(run['id']))
        for delay in [10,30,90,300,900]:
            self.now += delay
            last=store.tick(run['id'])
        self.assertEqual(last['status'],'blocked')
        self.assertEqual(last['actions'],[])

    def test_pause_cancels_pending_external_action(self):
        cancelled=[]
        store=self.store(capability_provider=self.write_catalog,
            executor=lambda g,a:{'pending':True,'remote_action_id':'remote'},poller=lambda g,a:{'pending':True},
            canceller=lambda g,a:cancelled.append(a['id']) or {'cancelled':True},
            planner=Planner(lambda c:decision('execute_resource',{
                'resource_id':'one','operation':'dm.reply','target':'dm:known','text':'Reply'})))
        run=store.create(self.goal(grants=[{'resource_id':'one','operations':['dm.reply'],'scope':{'conversation_ids':['known']}}]))
        active=store.tick(run['id']);paused=store.control(run['id'],'pause')
        self.assertEqual(cancelled,[active['actions'][0]['id']])
        self.assertEqual(paused['status'],'paused')
        self.assertIsNone(store.tick(run['id']))

    def test_grant_revocation_cancels_only_matching_pending_action(self):
        cancelled=[]
        store=self.store(capability_provider=self.write_catalog,
            executor=lambda g,a:{'pending':True},poller=lambda g,a:{'pending':True},
            canceller=lambda g,a:cancelled.append(a['id']) or {'cancelled':True},
            planner=Planner(lambda c:decision('execute_resource',{
                'resource_id':'one','operation':'dm.reply','target':'dm:known','text':'Reply'})))
        run=store.create(self.goal(grants=[{'resource_id':'one','operations':['dm.reply'],'scope':{'conversation_ids':['known']}}]))
        store.tick(run['id']);store.update_grants(run['id'],[])
        self.assertEqual(len(cancelled),1)
        final=self.advance(store,run)
        self.assertEqual(final['actions'][0]['status'],'cancelled')

    def test_changed_feedback_allows_refreshed_read(self):
        planner=Planner(lambda c:decision('read_resource',{'resource_id':'one','operation':'product.read'}))
        store=self.store(capability_provider=self.catalog,executor=lambda g,a:{'items':[]},planner=planner)
        run=store.create(self.goal(grants=[{'resource_id':'one','operations':['product.read']}]))
        store.tick(run['id']);store.wake(run['id'],'user_feedback',{'text':'Check updated information'})
        final=store.tick(run['id'])
        self.assertEqual(len(final['actions']),2)
        self.assertTrue(all(a['status']=='completed' for a in final['actions']))

    def test_new_feedback_wakes_wait_and_changes_planned_action_after_restart(self):
        seen=[]
        def plan(context):
            seen.append(context)
            if any(o['kind']=='user_feedback' for o in context['observations']):
                return decision('read_resource',{'resource_id':'one','operation':'product.read'})
            return decision('wait',{'seconds':3600})
        store=self.store(capability_provider=self.catalog,executor=lambda g,a:{'items':[]},planner=Planner(plan))
        run=store.create(self.goal(grants=[{'resource_id':'one','operations':['product.read']}]))
        waiting=store.tick(run['id'])
        self.assertEqual(waiting['actions'][-1]['kind'],'wait')
        self.assertIsNone(store.tick(run['id']))
        self.now += 1
        store.wake(run['id'],'user_feedback',{'text':'New observation requires checking the product'})
        reopened=self.store(capability_provider=self.catalog,executor=lambda g,a:{'items':[]},planner=Planner(plan))
        refreshed=reopened.tick(run['id'])
        self.assertEqual(refreshed['actions'][-1]['kind'],'read_resource')
        self.assertTrue(any(o['kind']=='user_feedback' for o in seen[-1]['observations']))
        self.assertEqual(refreshed['actions'][-1]['status'],'completed')

    def test_feedback_does_not_resume_paused_goal(self):
        planner=Planner(lambda c:decision('wait',{'seconds':3600}))
        store=self.store(planner=planner);run=store.create(self.goal())
        store.control(run['id'],'pause')
        store.wake(run['id'],'user_feedback',{'text':'New feedback'})
        self.assertIsNone(store.tick(run['id']))
        self.assertEqual(planner.calls,0)
        self.assertEqual(store.get(run['id'])['status'],'paused')

    def test_owner_business_confirmation_requires_same_goal_actual_observation(self):
        store=self.store()
        run=store.create(self.goal());other=store.create(self.goal())
        foreign=store.observe(other['id'],'resource_read',{'source':'cached own account'})
        draft=store.observe(run['id'],'artifact',{'content':'A claim of sale'})
        feedback=store.observe(run['id'],'user_feedback',{'text':'sale happened'})
        for evidence in (foreign,draft,feedback):
            with self.assertRaises(AutonomyError):store.confirm_delivery(run['id'],[evidence],'I verified delivery')
        observed=store.observe(run['id'],'external_receipt',{'receipt':{'status':'executed'}})
        final=store.confirm_delivery(run['id'],[observed],'I independently verified the buyer agreement and physical delivery')
        self.assertEqual(final['status'],'completed')
        self.assertTrue(any(x['kind']=='human_confirmation' for x in final['evidence']))
        with self.assertRaises(AutonomyError):store.confirm_delivery(run['id'],[observed],'Repeat')

    def test_pending_planning_does_not_consume_action_budget(self):
        def pending(c):raise PlanningPending('Pending')
        store=self.store(planner=Planner(pending));run=store.create(self.goal())
        result=store.tick(run['id'])
        self.assertEqual(result['status'],'active')
        self.assertEqual(result['actions'],[])
        self.assertGreater(result['next_check'],self.now)

if __name__=='__main__':unittest.main()
