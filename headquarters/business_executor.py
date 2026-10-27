"""Authenticated workbench business tasks; Channel is not the business scheduler."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import quote, urlsplit

import requests


class BusinessRequestRejected(ValueError):
    """A definitive API rejection, rather than an unknown transport outcome."""


class BusinessClient:
    def __init__(self, config):
        self.config = config
        self.base = config['base_url'].rstrip('/')
        parsed = urlsplit(self.base)
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1','localhost','::1')):
            raise ValueError('Workbench requires HTTPS or loopback')
        self.sdk = None

    def request(self, method, path, data=None, account=None):
        if not path.startswith('/api/business/'):
            raise ValueError('Only business API paths are allowed')
        if self.config.get('project'):
            if self.sdk is None:
                filename = Path(__file__).resolve().parents[2]/'agent-tools/leo-project-ops/scripts/leo_api.py'
                spec = importlib.util.spec_from_file_location('business_workspace_sdk',filename)
                self.sdk = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.sdk)
            result=self.sdk.api(self.config['project'],path,method,data,account_id=account)
            if result['status'] not in (200,201,202):
                if 400 <= result['status'] < 500:
                    raise BusinessRequestRejected('Workbench rejected business request')
                raise ValueError('Workbench business request not confirmed')
            return result['data']
        config = json.loads(Path(self.config['credential_file']).read_text())
        token = Path(config['token_file']).read_text().strip() if config.get('token_file') else config['token']
        with requests.Session() as session:
            if urlsplit(self.base).hostname in ('127.0.0.1','localhost','::1'):
                session.trust_env=False
            response=session.request(method,self.base+path,json=data,headers={'Authorization':'Bearer '+token},timeout=(3,15),allow_redirects=False)
        if response.status_code not in (200,201,202):
            if 400 <= response.status_code < 500:
                raise BusinessRequestRejected('Workbench rejected business request')
            raise ValueError('Workbench business request not confirmed')
        return response.json()


class WorkbenchGoalExecutor:
    use_business_tasks = True

    def __init__(self, directory, clients, legacy=None, channels=None, agents_provider=None):
        Path(directory).mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=Path(directory)/'workbench-business.sqlite3'
        self.clients=clients; self.legacy=legacy; self.channels=channels or {}; self.lock=threading.RLock()
        self.agents_provider=agents_provider
        self.cached=None;self.expires=0;self.last_pump=0
        with self.db() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS bindings(action_id TEXT PRIMARY KEY,goal_id TEXT NOT NULL,workbench TEXT NOT NULL,account_id TEXT NOT NULL,operation TEXT NOT NULL,request TEXT NOT NULL,task_id TEXT,status TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS cursors(workbench TEXT PRIMARY KEY,cursor INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS events(workbench TEXT NOT NULL,cursor INTEGER NOT NULL,goal_id TEXT NOT NULL,PRIMARY KEY(workbench,cursor));
              CREATE TABLE IF NOT EXISTS inbox(workbench TEXT NOT NULL,cursor INTEGER NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,PRIMARY KEY(workbench,cursor));
            ''')
        self.path.chmod(0o600)

    def db(self):
        db=sqlite3.connect(self.path,timeout=20);db.row_factory=sqlite3.Row;return db

    def __call__(self):
        with self.lock:
            if self.cached is not None and self.expires>time.time():return self.cached
            resources=[];errors=[];agents=[]
            for platform,client in self.clients.items():
                try:
                    catalog=client.request('GET','/api/business/capabilities')
                    for raw in catalog.get('resources',[]):
                        account=str(raw['account_id'])
                        # Preserve existing resource IDs from the old read-only catalog.
                        key=hashlib.sha256(json.dumps(['workbench',platform,account]).encode()).hexdigest()[:24]
                        resources.append(dict(raw,id=key,account_id=account,source_id=platform,channel_id='workbench',workbench_id=platform))
                except Exception:
                    errors.append({'platform':platform,'reason':'业务任务接口尚未确认可用'})
            # Reuse the native identity directory for planning/UI metadata only.
            # Business execution remains exclusively with the workbenches.
            for channel in self.channels.values():
                if not hasattr(channel,'catalog'):continue
                try:
                    for agent in channel.catalog().get('agents',[]):
                        if not any(a['id']==agent.get('id') for a in agents) and agent.get('id'):
                            agents.append({k:agent.get(k) for k in ('id','name','runtime_bound','runtime_availability','status')})
                except Exception:pass
            if self.agents_provider:
                try:
                    for agent in self.agents_provider():
                        if agent.get('id') and not agent.get('archived_at') and not any(a['id']==agent['id'] for a in agents):
                            agents.append({k:agent.get(k) for k in ('id','name','runtime_bound','runtime_availability','status')})
                except Exception:pass
            self.cached={'resources':resources,'agents':agents,'business_tasks':True,'workbench_errors':errors}
            self.expires=time.time()+10
            return self.cached

    def selected(self,goal,args):
        resource=next((r for r in self()['resources'] if r['id']==args.get('resource_id')),None)
        grant=next((g for g in goal.get('grants',[]) if g['resource_id']==args.get('resource_id') and args.get('operation') in g['operations']),None)
        if goal['status']!='active' or not resource or not grant or grant['expires_at']<=time.time() or goal['deadline']<=time.time():
            raise ValueError('Goal account authority is not active')
        available=next((o for o in resource['operations'] if o['id']==args.get('operation') and o.get('available')),None)
        if not available:raise ValueError('Workbench operation unavailable')
        return resource,grant

    def authority(self,goal,platform):
        resources={r['id']:r for r in self()['resources'] if r['source_id']==platform}
        grants=[]
        active=[]
        for g in goal.get('grants',[]):
            r=resources.get(g['resource_id'])
            if not r and g.get('source_id')==platform:r={'account_id':g['account_id']}
            if r:
                grants.append({'account_id':r['account_id'],'operations':g['operations'],'scope':g.get('scope',{}),'expires_at':g['expires_at']})
                if g.get('expires_at',0)>time.time():active.append(g['expires_at'])
        return {'revision':goal['revision'],'enabled':goal['status']=='active','expires_at':min(time.time()+30,goal['deadline'],max(active,default=goal['deadline'])),'grants':grants,'project_id':goal.get('project_id'),'origin':'headquarters'}

    def sync_goal(self,goal,platform=None):
        # Pause/revoke uses the latest exact grant set, never widens account permissions.
        errors=[]
        for name,client in self.clients.items():
            if platform and name!=platform:continue
            with self.db() as db:bound=db.execute('SELECT 1 FROM bindings WHERE goal_id=? AND workbench=?',(goal['id'],name)).fetchone()
            if not bound and not any(g.get('source_id')==name for g in goal.get('grants',[])):continue
            try:client.request('PUT','/api/business/goals/'+quote(goal['id'],safe='')+'/authority',self.authority(goal,name))
            except Exception:
                if platform:raise
                errors.append(name)
        return {'unconfirmed_platforms':errors}

    @staticmethod
    def result(task,platform,resource_id,operation):
        state=task.get('status',task.get('state'))
        pending=state in ('queued','pending','running','waiting_model','waiting_platform')
        return {'pending':pending,'status':'executed' if state in ('succeeded','completed') else 'uncertain' if state in ('uncertain','unknown') else 'failed' if not pending else 'pending',
            'business_task_id':str(task['id']),'workbench_id':platform,'resource_id':resource_id,'operation':operation,'task_state':state,
            'evidence':{'source':'authenticated_workbench_business_task','result':task.get('result'),'error':task.get('error'),'task_id':str(task['id'])}}

    def execute(self,goal,action):
        args=action['arguments'];resource,grant=self.selected(goal,args)
        platform=resource['source_id'];client=self.clients[platform]
        authority=self.authority(goal,platform)
        client.request('PUT','/api/business/goals/'+quote(goal['id'],safe='')+'/authority',authority)
        source=resource.get('channel_source_id');channel_account=resource.get('channel_account_id')
        if source and channel_account and goal.get('project_id') and goal.get('main_issue_id'):
            # Narrow metadata binding; does not submit or execute the business action.
            if not self.channels:raise ValueError('Model project binding service unavailable')
            next(iter(self.channels.values())).request('PUT','/control/v1/business-project-bindings',{
                'source_id':source,'account_id':channel_account,'message_id':'business-'+action['id'],
                'action_id':action['id'],'goal_id':goal['id'],'project_id':goal['project_id'],'parent_issue_id':goal['main_issue_id']})
        inputs=args.get('input',{})
        if action['kind']!='business_task':
            inputs={k:v for k,v in args.items() if k not in ('resource_id','operation','request_id')}
        body={'request_id':action['id'],'goal_id':goal['id'],'project_id':goal.get('project_id'),'action_id':action['id'],
            'account_id':resource['account_id'],'operation':args['operation'],'input':inputs,'authorization_revision':goal['revision'],'origin':'headquarters'}
        with self.db() as db:
            prior=db.execute('SELECT * FROM bindings WHERE action_id=?',(action['id'],)).fetchone()
            encoded=json.dumps(body,sort_keys=True,separators=(',',':'))
            if prior and prior['request']!=encoded:raise ValueError('Business request identity conflict')
            if not prior:
                db.execute('INSERT INTO bindings VALUES(?,?,?,?,?,?,?,?)',(action['id'],goal['id'],platform,resource['account_id'],args['operation'],encoded,None,'intent'))
        # An uncertain submit can safely retry this same ID only because the workbench
        # persists and fingerprints it before any platform write.
        try:
            task=client.request('POST','/api/business/tasks',body,resource['account_id'])
        except BusinessRequestRejected:
            with self.db() as db:db.execute("UPDATE bindings SET status='rejected' WHERE action_id=?",(action['id'],))
            return {'pending':False,'status':'failed','workbench_id':platform,'resource_id':resource['id'],'operation':args['operation'],'error':'Workbench rejected business task; no confirmed execution'}
        except Exception:
            return {'pending':True,'status':'pending','business_task_id':None,'workbench_id':platform,'resource_id':resource['id'],'operation':args['operation'],'submission':'unconfirmed; reconcile same request id'}
        self.verify(task,body)
        with self.db() as db:db.execute('UPDATE bindings SET task_id=?,status=? WHERE action_id=?',(str(task['id']),task.get('status',task.get('state')),action['id']))
        return self.result(task,platform,resource['id'],args['operation'])

    @staticmethod
    def verify(task,body):
        for key in ('goal_id','action_id','account_id','operation'):
            if str(task.get(key))!=str(body[key]):raise ValueError('Workbench task authority mismatch')
        if task.get('project_id')!=body.get('project_id'):raise ValueError('Workbench project association mismatch')

    def poll(self,goal,action):
        result=action['result']
        if not result.get('workbench_id'):
            # Read-only migration: reconcile previous Channel claims, never dispatch anew.
            if self.legacy:return self.legacy.poll(goal,action)
            raise ValueError('Legacy action needs explicit reconciliation')
        platform=result['workbench_id'];client=self.clients[platform]
        self.sync_goal(goal,platform)
        with self.db() as db:row=db.execute('SELECT * FROM bindings WHERE action_id=?',(action['id'],)).fetchone()
        if not row:raise ValueError('Business task association is missing')
        body=json.loads(row['request'])
        if row['task_id']:
            task=client.request('GET','/api/business/tasks/'+quote(row['task_id'],safe=''),account=row['account_id'])
        else:
            # Already persisted unconfirmed intent. No new request or model decision.
            task=client.request('POST','/api/business/tasks',body,row['account_id'])
        self.verify(task,body)
        with self.db() as db:db.execute('UPDATE bindings SET task_id=?,status=? WHERE action_id=?',(str(task['id']),task.get('status',task.get('state')),action['id']))
        return self.result(task,platform,result['resource_id'],result['operation'])

    def cancel(self,goal,action):
        result=action['result']
        if not result.get('workbench_id'):
            return self.legacy.cancel(goal,action) if self.legacy else {'cancelled':False}
        self.sync_goal(goal,result['workbench_id'])
        if result.get('business_task_id'):
            return self.clients[result['workbench_id']].request('POST','/api/business/tasks/'+quote(result['business_task_id'],safe='')+'/cancel',{})
        return {'cancelled':False,'reason':'Submission reconciliation pending; authority withdrawn'}

    def pump_events(self,store):
        if time.time()-self.last_pump<5:return
        self.last_pump=time.time()
        for goal in store.list():
            if goal['status']=='active':
                try:self.sync_goal(goal)
                except Exception:pass
        for platform,client in self.clients.items():
            with self.db() as db:row=db.execute('SELECT cursor FROM cursors WHERE workbench=?',(platform,)).fetchone()
            cursor=row[0] if row else 0
            try:data=client.request('GET','/api/business/events?after='+str(cursor))
            except Exception:continue
            with self.db() as db:
                for event in data.get('items',data.get('events',[])):
                    db.execute('INSERT OR IGNORE INTO inbox VALUES(?,?,?,?)',(platform,int(event['cursor']),json.dumps(event),'pending'))
                db.execute('INSERT OR REPLACE INTO cursors VALUES(?,?)',(platform,int(data.get('cursor',data.get('next_cursor',cursor)))))
                pending=db.execute("SELECT * FROM inbox WHERE workbench=? AND status='pending' ORDER BY cursor LIMIT 100",(platform,)).fetchall()
            for incoming in pending:
                event=json.loads(incoming['payload'])
                eid=int(event['cursor'])
                with self.db() as db:
                    if db.execute('SELECT 1 FROM events WHERE workbench=? AND cursor=?',(platform,eid)).fetchone():continue
                    binding=db.execute('SELECT * FROM bindings WHERE workbench=? AND goal_id=? AND action_id=?',
                        (platform,event.get('goal_id'),event.get('action_id'))).fetchone()
                if not binding:
                    with self.db() as db:db.execute("UPDATE inbox SET status='ignored' WHERE workbench=? AND cursor=?",(platform,eid))
                    continue
                if not binding['task_id']:
                    try:
                        task=client.request('GET','/api/business/tasks/'+quote(str(event.get('task_id')),safe=''),account=binding['account_id'])
                        self.verify(task,json.loads(binding['request']))
                    except Exception:continue
                    with self.db() as db:db.execute('UPDATE bindings SET task_id=? WHERE action_id=?',(str(task['id']),binding['action_id']))
                elif binding['task_id']!=str(event.get('task_id')):continue
                goal=store.get(binding['goal_id'])
                if event.get('project_id')!=goal.get('project_id') or str(event.get('account_id',binding['account_id']))!=binding['account_id']:continue
                # External observations remain observations, never a self-certified sale.
                store.ingest_business_event(platform,str(eid),goal['id'],{'source':'authenticated_workbench_event','workbench':platform,'task_id':str(event['task_id']),'kind':event.get('kind'),'payload':event.get('payload',{})})
                with self.db() as db:
                    db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?)',(platform,eid,goal['id']))
                    db.execute("UPDATE inbox SET status='done' WHERE workbench=? AND cursor=?",(platform,eid))
