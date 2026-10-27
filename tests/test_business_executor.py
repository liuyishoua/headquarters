import tempfile
import time
import unittest
from headquarters.autonomy import AutonomyStore
from headquarters.business_executor import WorkbenchGoalExecutor


class Workbench:
    def __init__(self):
        self.tasks={};self.events=[];self.submissions=0;self.authorities={}
    def request(self,method,path,data=None,account=None):
        if path.endswith('/capabilities'):
            return {'resources':[{'account_id':'a','name':'Synthetic a','operations':[{'id':'product.read','available':True,'parameters':{}}]},
                {'account_id':'b','name':'Synthetic b','operations':[{'id':'conversation.read','available':True,'parameters':{}}]}]}
        if '/authority' in path:self.authorities[path]=data;return {'ok':True}
        if path=='/api/business/tasks':
            self.submissions+=1
            if data['request_id'] not in self.tasks:self.tasks[data['request_id']]={**data,'id':'task-'+data['request_id'],'status':'queued','result':None}
            return self.tasks[data['request_id']]
        if '/events?' in path:
            after=int(path.split('after=')[1]);return {'items':[x for x in self.events if x['cursor']>after],'cursor':len(self.events)}
        if '/tasks/' in path:
            task=next(t for t in self.tasks.values() if t['id']==path.split('/')[-1]);task['status']='succeeded';task['result']={'source':'synthetic_platform','items':[{'finding':'new observation'}]};return task
        raise AssertionError(path)


class BusinessExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.now=time.time()
        self.web=Workbench();self.adapter=WorkbenchGoalExecutor(self.tmp.name,{'synthetic':self.web})
    def goal(self,planner):
        store=AutonomyStore(self.tmp.name,clock=lambda:self.now,planner=planner,capability_provider=self.adapter,executor=self.adapter.execute,poller=self.adapter.poll,canceller=self.adapter.cancel)
        resource=self.adapter()['resources'][0]
        run=store.create({'title':'Synthetic goal','objective':'Observe business results','completion_criteria':['Actual result'],'grants':[{'resource_id':resource['id'],'operations':['product.read'],'scope':{'include_goal_created':True}}]})
        return store,run,resource
    def test_async_read_is_observed_only_after_business_result_and_not_resubmitted(self):
        class Planner:
            def plan(inner,c):
                resource=c['grants'][0]['resource_id']
                return {'kind':'business_task','arguments':{'resource_id':resource,'operation':'product.read','input':{}},'summary':'Read own products','rationale':'Synthetic safe read'}
        store,run,_=self.goal(Planner());first=store.tick(run['id'])
        self.assertEqual(first['actions'][0]['status'],'pending');self.assertFalse(first['evidence'])
        self.now+=11;final=store.tick(run['id'])
        self.assertEqual(final['actions'][0]['status'],'completed');self.assertEqual(self.web.submissions,1)
        self.assertEqual(final['evidence'][0]['kind'],'resource_read')
        self.assertEqual(final['actions'][0]['result']['evidence']['result']['source'],'synthetic_platform')
    def test_event_association_dedup_restart_and_cross_goal_rejection(self):
        class Planner:
            def plan(inner,c):return {'kind':'business_task','arguments':{'resource_id':c['grants'][0]['resource_id'],'operation':'product.read','input':{}},'summary':'Read','rationale':'Observe'}
        store,run,_=self.goal(Planner());store.tick(run['id']);task=next(iter(self.web.tasks.values()))
        event={k:task[k] for k in ('goal_id','project_id','action_id','account_id')};event.update(cursor=1,task_id=task['id'],kind='incoming',payload={'synthetic':True})
        self.web.events=[event];self.adapter.pump_events(store)
        count=len(store.get(run['id'])['observations']);self.assertEqual(count,1)
        self.adapter=WorkbenchGoalExecutor(self.tmp.name,{'synthetic':self.web});self.adapter.pump_events(store)
        self.assertEqual(len(store.get(run['id'])['observations']),count)
        # Force replay after cursor persistence loss; the goal ledger still deduplicates.
        with self.adapter.db() as db:db.execute('DELETE FROM cursors');db.execute('DELETE FROM events')
        self.adapter.last_pump=0;self.adapter.pump_events(store)
        self.assertEqual(len(store.get(run['id'])['observations']),count)
        bad=dict(event,cursor=2,goal_id='foreign');self.web.events.append(bad)
        self.adapter.last_pump=0;self.adapter.pump_events(store)
        self.assertEqual(len(store.get(run['id'])['observations']),count)

    def test_unconfirmed_submit_recovers_event_binding_before_cursor_moves(self):
        class Planner:
            def plan(inner,c):return {'kind':'business_task','arguments':{'resource_id':c['grants'][0]['resource_id'],'operation':'product.read','input':{}},'summary':'Read','rationale':'Observe'}
        original=self.web.request
        def lost(method,path,data=None,account=None):
            response=original(method,path,data,account)
            if path=='/api/business/tasks':raise TimeoutError('Synthetic lost response')
            return response
        self.web.request=lost
        store,run,_=self.goal(Planner());first=store.tick(run['id'])
        self.assertIsNone(first['actions'][0]['result']['business_task_id'])
        task=next(iter(self.web.tasks.values()));event={k:task[k] for k in ('goal_id','project_id','action_id','account_id')}
        event.update(cursor=1,task_id=task['id'],kind='incoming',payload={'synthetic':True});self.web.events=[event]
        self.adapter.pump_events(store)
        self.assertEqual(len(store.get(run['id'])['observations']),1)
        with self.adapter.db() as db:binding=db.execute('SELECT task_id FROM bindings').fetchone()
        self.assertEqual(binding[0],task['id']);self.assertEqual(self.web.submissions,1)
        reopened=WorkbenchGoalExecutor(self.tmp.name,{'synthetic':self.web});reopened.pump_events(store)
        self.assertEqual(len(store.get(run['id'])['observations']),1)

    def test_expired_grant_and_other_workbench_failure_do_not_block_selected_platform(self):
        class Offline:
            def request(self,*a,**kw):raise TimeoutError('Synthetic offline workbench')
        self.adapter=WorkbenchGoalExecutor(self.tmp.name,{'synthetic':self.web,'offline':Offline()})
        class Planner:
            def plan(inner,c):return {'kind':'business_task','arguments':{'resource_id':c['grants'][0]['resource_id'],'operation':'product.read','input':{}},'summary':'Read','rationale':'Observe'}
        store,run,_=self.goal(Planner());copy=dict(run,grants=run['grants']+[{'resource_id':'expired','source_id':'synthetic','account_id':'b','operations':['conversation.read'],'expires_at':time.time()-1}])
        auth=self.adapter.authority(copy,'synthetic')
        self.assertEqual(len(auth['grants']),2);self.assertGreater(auth['expires_at'],time.time()+20)
        self.assertLess(auth['grants'][1]['expires_at'],time.time())
        first=store.tick(run['id']);self.now+=11;final=store.tick(run['id'])
        self.assertEqual(final['actions'][0]['status'],'completed');self.assertEqual(self.web.submissions,1)

    def test_native_identity_metadata_is_reused_without_runtime_secrets(self):
        self.adapter=WorkbenchGoalExecutor(self.tmp.name,{'synthetic':self.web},agents_provider=lambda:[
            {'id':'leo','name':'小红书-leo','custom_env':{'SECRET':'synthetic'},'mcp_config':{'private':'synthetic'}},
            {'id':'retired','name':'Archived test identity','archived_at':'synthetic'}])
        agents=self.adapter()['agents']
        self.assertEqual([a['name'] for a in agents],['小红书-leo'])
        self.assertNotIn('custom_env',agents[0]);self.assertNotIn('mcp_config',agents[0])


if __name__=='__main__':unittest.main()
