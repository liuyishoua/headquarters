import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'channel'))
from channel_service.app import create_app as channel_app
from channel_service.demo import DemoMultica, AGENT_ID
from headquarters.app import create_app
from headquarters.core import Headquarters, Problem

class Model(DemoMultica):
    def _run(self, issue_id, route, comment_id=None):
        super()._run(issue_id, route, comment_id)
        self.task_rows[issue_id][-1]['result'] = {'content': json.dumps(dict(summary='虚构咨询已答复', requires_human=False, action=dict(type='dm.reply', text='示例商品可以包邮，请问需要什么规格？'), followup_after_seconds=300), ensure_ascii=False)}

class Client:
    def __init__(self, app, directory):
        self.app = app; self.key = (Path(directory) / 'command-key').read_text().strip()
        self.online = True; self.lose_ack = False; self.renew_calls = 0
    def call(self, method, path, data=None):
        if not self.online: raise OSError('synthetic offline channel')
        response = self.app.test_client().open(path, method=method, json=data, headers={'Authorization': 'Bearer ' + self.key})
        if response.status_code != 200: raise RuntimeError(str(response.json))
        if self.lose_ack and '/deployments/' in path:
            self.lose_ack = False; raise OSError('synthetic lost acknowledgement after commit')
        return response.json
    def catalog(self): return self.call('GET', '/control/v1/catalog')
    def state(self, after): return self.call('GET', '/control/v1/state?after=' + str(after))
    def deploy(self, goal_id, data): return self.call('PUT', '/control/v1/deployments/' + goal_id, data)
    def decision(self, action_id, approve): return self.call('POST', '/control/v1/actions/' + action_id + '/decision', {'approve': approve})
    def renew(self, rows):
        self.renew_calls += 1
        return self.call('POST', '/control/v1/renewals', {'items': rows})

class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.clients = {}
        for key in ('east', 'west'):
            directory = Path(self.temp.name) / key; app = channel_app(directory, Model(), auth=False, demo=True)
            for source in ('xianyu', 'weibo'): app.extensions['channel'].source(source)
            app.extensions['automation'].heartbeat('xianyu' if key == 'east' else 'weibo', dict(account_id='synthetic-' + key, executor_id='executor', capabilities=['dm.reply']))
            self.clients[key] = Client(app, directory)
        self.hq = Headquarters(Path(self.temp.name) / 'hq', self.clients); self.hq.tick()
        self.accounts = {key: self.hq.save_account(dict(channel_id=key, source_id='xianyu' if key == 'east' else 'weibo', account_id='synthetic-' + key, name='虚构账号-' + key, hour_start=0, hour_end=24, min_interval=10))['id'] for key in self.clients}
        self.counter = 0
    def mission(self, metric='outcome', target=1, stages=1, keys=('east',), mode='automatic', name='虚构客服目标'):
        mission = self.hq.save_mission(dict(name=name, objective='仅使用虚构信息处理咨询，完成后停止。', knowledge='示例商品支持包邮。', stages=[dict(id='stage-'+str(n), name='阶段-'+str(n), objective='回答虚构问题', criterion=dict(metric=metric, kind='resolved' if metric == 'outcome' else 'dm.reply', target=target)) for n in range(stages)]))
        for stage in mission['config']['stages']:
            for key in keys: self.hq.assign(mission['id'], dict(stage_id=stage['id'], account_id=self.accounts[key], agent_id=AGENT_ID, mode=mode, role='客服与跟进'))
        return self.hq.mission(mission['id'])
    def start(self, mission):
        self.hq.transition(mission['id'], 'start'); self.hq.tick(); self.hq.tick(); return self.hq.mission(mission['id'])
    def event(self, key='east', conversation='customer', text='虚构商品可以包邮吗？', event_id=None):
        self.counter += 1
        return self.clients[key].app.extensions['automation'].ingest('xianyu' if key=='east' else 'weibo', dict(account_id='synthetic-'+key,event_id=event_id or 'message-'+str(self.counter),event_type='dm.received',conversation_id=conversation,content={'text':text}))[0]
    def process(self, key='east'):
        app = self.clients[key].app; auto,store,worker = (app.extensions[x] for x in ('automation','channel','worker'))
        for _ in range(8):
            auto.tick()
            with store.db(True) as db: db.execute('UPDATE batches SET next_poll=0,lease_until=0')
            batch = store.claim()
            if batch: worker.process(batch)
        auto.tick(); self.hq.tick(); return auto.actions()[0]
    def receipt(self,key='east',external_id=None):
        auto=self.clients[key].app.extensions['automation']; source='xianyu' if key=='east' else 'weibo'
        action=auto.claim(source,dict(account_id='synthetic-'+key,executor_id='executor'))['action']; self.assertIsNotNone(action)
        auto.receipt(source,action['id'],dict(account_id='synthetic-'+key,claim_token=action['claim_token'],status='succeeded',external_id=external_id or 'platform-'+action['id'])); self.hq.tick(); return action
    def evidence(self,mission,action,key='east',subject='customer',**changes):
        data=dict(kind='resolved',subject=subject,channel_id=key,action_id=action['id'],note='人工核实：虚构客户已确认咨询解决。');data.update(changes);return self.hq.outcome(mission['id'],data)
    def test_one_goal_two_channels_two_platforms(self):
        m=self.start(self.mission(metric='receipt',target=2,keys=('east','west')))
        for k in self.clients: self.assertEqual(len(self.event(k)['jobs']),1);self.process(k);self.receipt(k)
        m=self.hq.mission(m['id']);self.assertEqual(m['progress']['stage-0'],2);self.assertEqual(m['stats']['succeeded'],2);self.assertEqual({a['channel_id'] for a in m['actions']},{'east','west'})
    def test_proposal_and_receipt_do_not_count_as_business_success(self):
        m=self.start(self.mission());self.event();a=self.process()
        with self.assertRaises(Problem):self.evidence(m,a)
        self.receipt();self.assertEqual(self.hq.mission(m['id'])['progress']['stage-0'],0)
        self.evidence(m,a);self.assertEqual(self.hq.mission(m['id'])['progress']['stage-0'],1)
    def test_cross_platform_business_key_deduplication(self):
        m=self.start(self.mission(keys=('east','west')))
        for k in self.clients:
            self.event(k);self.process(k);a=self.receipt(k)
            if k=='east':self.evidence(m,a,k,subject='explicit-shared-customer')
            else:
                with self.assertRaises(Problem):self.evidence(m,a,k,subject='explicit-shared-customer')
        self.assertEqual(self.hq.mission(m['id'])['progress']['stage-0'],1)
    def test_outcome_retry_and_evidence_cannot_inflate_customers(self):
        m=self.start(self.mission());self.event();self.process();a=self.receipt();first=self.evidence(m,a);second=self.evidence(m,a)
        self.assertEqual(first['id'],second['id']);self.assertTrue(second['duplicate'])
        with self.assertRaises(Problem):self.evidence(m,a,subject='invented-customer')
    def test_wrong_mission_evidence_rejected(self):
        m=self.start(self.mission());other=self.mission(name='另一个目标');self.event();self.process();a=self.receipt()
        with self.assertRaises(Problem):self.evidence(other,a)
    def test_same_nickname_keeps_platform_tasks_distinct(self):
        m=self.start(self.mission(keys=('east','west')))
        for k in self.clients:self.event(k,conversation='same-name',event_id='same-id');self.process(k)
        actions=self.hq.mission(m['id'])['actions'];self.assertEqual(len(actions),2);self.assertNotEqual(actions[0]['case_id'],actions[1]['case_id'])
    def test_same_conversation_appends_multica_task(self):
        self.start(self.mission());self.event();self.process();self.receipt();self.event(text='还有蓝色吗？');self.process()
        self.assertEqual([c[0] for c in self.clients['east'].app.extensions['multica'].calls],['create','append'])
    def test_same_account_competing_goals_blocked(self):
        first=self.start(self.mission());second=self.mission(name='争用目标')
        with self.assertRaisesRegex(Problem,'争用'):self.hq.transition(second['id'],'start')
        self.hq.transition(first['id'],'pause');self.hq.tick();self.hq.transition(second['id'],'start')
    def test_parallel_activation_one_winner(self):
        missions=[self.mission(name='并发目标-'+str(n)) for n in range(2)]
        def start(m):
            try:self.hq.transition(m['id'],'start');return True
            except Problem:return False
        with concurrent.futures.ThreadPoolExecutor(2) as pool:self.assertEqual(sum(pool.map(start,missions)),1)
    def test_local_channel_rule_conflict_visible(self):
        self.clients['east'].app.extensions['automation'].save_goal(dict(source_id='xianyu',account_id='synthetic-east',agent_id=AGENT_ID,name='已有本地规则',objective='本地业务',enabled=True));self.hq.tick();m=self.mission()
        with self.assertRaisesRegex(Problem,'本地规则'):self.hq.transition(m['id'],'start')
    def test_offline_connector_blocks_start(self):
        with self.clients['east'].app.extensions['channel'].db(True) as db:db.execute('UPDATE account_connectors SET last_seen=0')
        self.hq.tick();m=self.mission()
        with self.assertRaisesRegex(Problem,'没有在线执行器'):self.hq.transition(m['id'],'start')
    def test_unsupported_connector_blocks_start(self):
        with self.clients['east'].app.extensions['channel'].db(True) as db:db.execute("UPDATE account_connectors SET capabilities='[]'")
        self.hq.tick();m=self.mission()
        with self.assertRaisesRegex(Problem,'不支持'):self.hq.transition(m['id'],'start')
    def test_unknown_agent_blocks_start(self):
        m=self.mission()
        with self.hq.db(True) as db:
            c=json.loads(db.execute('SELECT config FROM assignments').fetchone()[0]);c['agent_id']='missing';db.execute('UPDATE assignments SET config=?',(json.dumps(c),))
        with self.assertRaisesRegex(Problem,'Agent'):self.hq.transition(m['id'],'start')
    def test_pause_cancels_unclaimed_and_timers(self):
        m=self.start(self.mission());self.event();self.process();self.receipt();self.event(text='第二次咨询');a=self.process();self.hq.transition(m['id'],'pause');self.hq.tick();auto=self.clients['east'].app.extensions['automation']
        self.assertEqual(next(x for x in auto.actions() if x['id']==a['id'])['state'],'cancelled')
        with auto.store.db() as db:self.assertEqual(db.execute("SELECT count(*) FROM followup_timers WHERE state='waiting'").fetchone()[0],0)
    def test_stage_evidence_gate_and_withdrawal(self):
        m=self.start(self.mission(metric='receipt',stages=2))
        with self.assertRaisesRegex(Problem,'完成标准'):self.hq.transition(m['id'],'advance')
        self.event();self.process();self.receipt();m=self.hq.transition(m['id'],'advance');self.assertEqual((m['state'],m['stage']),('paused',1));self.assertTrue(all(not d['payload']['goal']['enabled'] for d in m['deployments']))
        self.hq.transition(m['id'],'start');self.hq.tick();goals=self.clients['east'].app.extensions['automation'].goals();self.assertEqual(sum(g['enabled'] for g in goals),1);self.assertIn('阶段-1',next(g['name'] for g in goals if g['enabled']))
    def test_final_completion_disables_goals(self):
        m=self.start(self.mission(metric='receipt'));self.event();self.process();self.receipt();final=self.hq.transition(m['id'],'advance');self.hq.tick();self.assertEqual(final['state'],'completed');self.assertFalse(any(g['enabled'] for g in self.clients['east'].app.extensions['automation'].goals()))
    def test_pause_survives_channel_outage(self):
        m=self.start(self.mission());self.clients['east'].online=False;self.hq.transition(m['id'],'pause');self.hq.tick();d=self.hq.mission(m['id'])['deployments'][0];self.assertNotEqual(d['applied'],d['revision']);self.assertTrue(d['error']);self.clients['east'].online=True;self.hq.tick();self.assertFalse(self.clients['east'].app.extensions['automation'].goals()[0]['enabled'])
    def test_lost_ack_reconciles_without_version_churn(self):
        m=self.mission();self.hq.transition(m['id'],'start');self.clients['east'].lose_ack=True;self.hq.tick();first=self.clients['east'].app.extensions['automation'].goals()[0];self.hq.tick();second=self.clients['east'].app.extensions['automation'].goals()[0];self.assertEqual((first['id'],first['version']),(second['id'],second['version']))
    def test_account_policy_applies_all_missions(self):
        missions=[self.mission(name='目标-'+str(n)) for n in range(2)];self.hq.save_account(dict(channel_id='east',source_id='xianyu',account_id='synthetic-east',name='虚构账号',daily_limit=7,min_interval=90))
        for m in missions:g=self.hq.mission(m['id'])['deployments'][0]['payload']['goal'];self.assertEqual((g['daily_limit'],g['min_interval']),(7,90))
    def test_duplicate_physical_account_across_channels_rejected(self):
        with self.assertRaisesRegex(Problem,'一个 Channel'):self.hq.save_account(dict(channel_id='west',source_id='xianyu',account_id='synthetic-east',name='重复'))
    def test_active_goal_cannot_rewrite_meaning(self):
        m=self.start(self.mission())
        with self.assertRaises(Problem):self.hq.save_mission(m['config'],m['id'])
    def test_executed_goal_cannot_rewrite_criterion(self):
        m=self.start(self.mission());self.event();self.process();self.hq.transition(m['id'],'pause')
        with self.assertRaises(Problem):self.hq.save_mission(m['config'],m['id'])
    def test_restart_preserves_receipts_without_duplicate_deployments(self):
        m=self.start(self.mission());self.event();self.process();self.receipt();recovered=Headquarters(self.hq.directory,self.clients);recovered.tick();self.assertEqual(len(recovered.mission(m['id'])['actions']),1);self.assertEqual(len(self.clients['east'].app.extensions['automation'].goals()),1)
    def test_batch_renewal_preserves_route_and_pending_action(self):
        self.start(self.mission());self.event();self.process();before=self.clients['east'].app.extensions['automation'].goals()[0]
        with self.hq.db(True) as db:db.execute('UPDATE deployments SET last_ack=0')
        self.hq.tick();after=self.clients['east'].app.extensions['automation'].goals()[0];self.assertEqual(before['version'],after['version']);self.assertEqual(self.clients['east'].renew_calls,1);self.assertEqual(self.clients['east'].app.extensions['automation'].actions()[0]['state'],'ready')
    def test_expired_authority_blocks_ingest_and_claim(self):
        self.start(self.mission());self.event();self.process();auto=self.clients['east'].app.extensions['automation']
        with auto.store.db(True) as db:db.execute('UPDATE control_goals SET valid_until=0')
        event=self.event();self.assertEqual(event['state'],'waiting_authority');self.assertEqual(len(event['jobs']),1);self.assertIsNone(auto.claim('xianyu',dict(account_id='synthetic-east',executor_id='executor'))['action'])
    def test_expired_authority_suspends_pending_model_job(self):
        self.start(self.mission());self.event();app=self.clients['east'].app;auto=app.extensions['automation']
        with auto.store.db(True) as db:db.execute('UPDATE control_goals SET valid_until=0')
        auto.tick();self.assertEqual(app.extensions['multica'].calls,[])
        with auto.store.db() as db:self.assertEqual(db.execute('SELECT state FROM goal_jobs').fetchone()[0],'pending')
    def test_local_editor_cannot_change_owned_projection(self):
        self.start(self.mission());auto=self.clients['east'].app.extensions['automation'];g=auto.goals()[0]
        with self.assertRaisesRegex(Exception,'指挥部'):auto.save_goal({**g,'enabled':False},g['id'])
    def test_old_enable_cannot_overtake_pause(self):
        m=self.start(self.mission());d=m['deployments'][0];self.hq.transition(m['id'],'pause');self.hq.tick()
        with self.assertRaises(RuntimeError):self.clients['east'].deploy(d['goal_id'],d['payload'])
        self.assertFalse(self.clients['east'].app.extensions['automation'].goals()[0]['enabled'])
    def test_same_revision_different_payload_rejected(self):
        m=self.start(self.mission());d=m['deployments'][0];payload=json.loads(json.dumps(d['payload']));payload['goal']['objective']='篡改目标'
        with self.assertRaises(RuntimeError):self.clients['east'].deploy(d['goal_id'],payload)
    def test_source_key_and_browser_origin_rejected_by_control(self):
        c=self.clients['east'];key=c.app.extensions['channel'].source('test-source')['token']
        for h in ({},{'Authorization':'Bearer '+key},{'Authorization':'Bearer '+c.key,'Origin':'https://browser.test'}):self.assertEqual(c.app.test_client().get('/control/v1/state',headers=h).status_code,401)
    def test_existing_local_goal_cannot_be_taken_over(self):
        m=self.mission();d=m['deployments'][0];g=self.clients['east'].app.extensions['automation'].save_goal(d['payload']['goal'])
        with self.assertRaises(RuntimeError):self.clients['east'].deploy(g['id'],d['payload'])
    def test_paged_changefeed_keeps_late_updates(self):
        m=self.start(self.mission());self.event();a=self.process();self.receipt();store=self.clients['east'].app.extensions['channel']
        with store.db(True) as db:
            for _ in range(600):db.execute('UPDATE account_actions SET reason=? WHERE id=?',('合成更新',a['id']))
        self.hq.tick();actions=self.hq.mission(m['id'])['actions'];self.assertEqual(len(actions),1);self.assertEqual(actions[0]['reason'],'合成更新')
    def test_review_adoption_from_headquarters(self):
        m=self.start(self.mission(mode='review'));self.event();a=self.process();self.assertEqual(a['state'],'review');self.hq.decide_action('east',a['id'],True);self.receipt();self.assertEqual(self.hq.mission(m['id'])['actions'][0]['state'],'succeeded')
    def test_offline_channel_does_not_block_healthy_channel(self):
        m=self.start(self.mission(keys=('east','west')));self.clients['east'].online=False;self.event('west');self.process('west');self.receipt('west');self.assertEqual(self.hq.mission(m['id'])['stats']['succeeded'],1);self.assertTrue(next(c for c in self.hq.overview()['channels'] if c['id']=='east')['error'])
    def test_stop_contact_survives_goal_switch(self):
        m=self.start(self.mission());self.event(text='不要再联系我了');self.hq.transition(m['id'],'pause');self.hq.tick();self.start(self.mission(name='另一个目标'));self.assertEqual(self.event()['state'],'stopped')
    def test_business_command_context_reaches_agent(self):
        m=self.start(self.mission());self.event();self.process();store=self.clients['east'].app.extensions['channel'];message=store.messages()[0];context=store.message(message['id'])['payload']['context'];self.assertEqual(context['command']['mission_id'],m['id']);self.assertEqual(context['command']['success_criterion']['metric'],'outcome')

    def test_events_wait_durably_during_lost_authority_and_resume(self):
        self.start(self.mission());auto=self.clients['east'].app.extensions['automation']
        with auto.store.db(True) as db:db.execute('UPDATE control_goals SET valid_until=0')
        event=self.event();auto.tick();self.assertEqual(event['state'],'waiting_authority')
        with self.hq.db(True) as db:db.execute('UPDATE deployments SET last_ack=0')
        self.hq.tick();action=self.process();self.assertEqual(action['state'],'ready')
    def test_previous_stage_verified_results_reach_next_stage_context(self):
        m=self.start(self.mission(stages=2));self.event();self.process();a=self.receipt();self.evidence(m,a)
        self.hq.transition(m['id'],'advance');self.hq.tick();self.hq.transition(m['id'],'start');self.hq.tick()
        current=next(d for d in self.hq.mission(m['id'])['deployments'] if d['payload']['goal']['enabled'])
        results=current['payload']['context']['previous_stage_results'];self.assertEqual(len(results),1);self.assertEqual(results[0]['subject'],'customer')
    def test_uncertain_action_prevents_stage_acceptance(self):
        m=self.start(self.mission(metric='receipt'));self.event();self.process();self.receipt();self.event(conversation='second-customer');self.process()
        auto=self.clients['east'].app.extensions['automation']
        with auto.store.db(True) as db:db.execute("UPDATE account_actions SET state='uncertain' WHERE state='ready'")
        self.hq.tick()
        with self.assertRaisesRegex(Problem,'待核对'):self.hq.transition(m['id'],'advance')
    def test_pending_old_withdrawal_blocks_new_stage_grant(self):
        m=self.start(self.mission(metric='receipt',stages=2,keys=('east','west')));self.event();self.process();self.receipt();self.hq.transition(m['id'],'advance');self.hq.transition(m['id'],'start')
        self.clients['east'].online=False;self.hq.tick()
        self.assertFalse(any(g['enabled'] for g in self.clients['west'].app.extensions['automation'].goals()))
    def test_late_receipt_updates_headquarters_after_pause(self):
        m=self.start(self.mission());self.event();self.process();auto=self.clients['east'].app.extensions['automation'];a=auto.claim('xianyu',dict(account_id='synthetic-east',executor_id='executor'))['action']
        self.hq.transition(m['id'],'pause');self.hq.tick();auto.receipt('xianyu',a['id'],dict(account_id='synthetic-east',claim_token=a['claim_token'],status='succeeded',external_id='late-synthetic-receipt'));self.hq.tick()
        self.assertEqual(self.hq.mission(m['id'])['actions'][0]['receipt']['external_id'],'late-synthetic-receipt')
    def test_replayed_platform_receipt_counted_once(self):
        m=self.start(self.mission(metric='receipt',target=2));self.event();self.process();self.receipt(external_id='one-platform-message');self.event(conversation='another');self.process()
        auto=self.clients['east'].app.extensions['automation']
        with auto.store.db(True) as db:db.execute('UPDATE account_actions SET lease_until=lease_until-100 WHERE claim_hash IS NOT NULL')
        self.receipt(external_id='one-platform-message');self.assertEqual(self.hq.mission(m['id'])['progress']['stage-0'],1)
    def test_central_pause_withdraws_all_running_missions(self):
        first=self.start(self.mission());second=self.start(self.mission(keys=('west',),name='另一个平台目标'))
        result=self.hq.pause_all();self.hq.tick();self.assertEqual(result['paused'],2)
        self.assertTrue(all(m['state']=='paused' for m in self.hq.overview()['missions']))
        self.assertFalse(any(g['enabled'] for c in self.clients.values() for g in c.app.extensions['automation'].goals()))
    def test_expiry_after_queueing_blocks_remote_model_write_until_renewal(self):
        self.start(self.mission());self.event();app=self.clients['east'].app;auto,store,worker=(app.extensions[x] for x in ('automation','channel','worker'));auto.tick();batch=store.claim()
        self.assertIsNotNone(batch)
        with store.db(True) as db:db.execute('UPDATE control_goals SET valid_until=0')
        worker.process(batch);self.assertEqual(app.extensions['multica'].calls,[]);self.assertFalse(store.batch(batch['id'])['write_started'])
        with self.hq.db(True) as db:db.execute('UPDATE deployments SET last_ack=0')
        self.hq.tick();worker.process(store.batch(batch['id']));self.assertEqual(len(app.extensions['multica'].calls),1)
    def test_pause_after_queueing_cancels_unwritten_model_batch(self):
        m=self.start(self.mission());self.event();app=self.clients['east'].app;auto,store,worker=(app.extensions[x] for x in ('automation','channel','worker'));auto.tick();batch=store.claim()
        self.hq.transition(m['id'],'pause');self.hq.tick();worker.process(batch);self.assertEqual(app.extensions['multica'].calls,[]);self.assertEqual(store.batch(batch['id'])['status'],'cancelled')
    def test_expiry_during_body_preparation_rechecked_before_write_intent(self):
        self.start(self.mission());self.event();app=self.clients['east'].app;auto,store,worker=(app.extensions[x] for x in ('automation','channel','worker'));auto.tick();batch=store.claim();original=worker.body
        def prepare(*args):
            body=original(*args)
            with store.db(True) as db:db.execute('UPDATE control_goals SET valid_until=0')
            return body
        worker.body=prepare;worker.process(batch);self.assertEqual(app.extensions['multica'].calls,[]);self.assertFalse(store.batch(batch['id'])['write_started'])
    def test_completed_mission_cannot_restart(self):
        m=self.start(self.mission(metric='receipt'));self.event();self.process();self.receipt();self.hq.transition(m['id'],'advance')
        with self.assertRaises(Problem):self.hq.transition(m['id'],'start')

class AuthTests(unittest.TestCase):
    def test_login_csrf_logout(self):
        with tempfile.TemporaryDirectory() as directory:
            app=create_app(directory,{},origin='https://hq.test');c=app.test_client();password=(Path(directory)/'admin-password').read_text().strip();headers={'Origin':'https://hq.test','X-App-Request':'1'}
            self.assertEqual(c.get('/api/overview').status_code,401);self.assertEqual(c.post('/api/login',json={'password':password}).status_code,403);self.assertEqual(c.post('/api/login',json={'password':password},headers=headers).status_code,200);self.assertEqual(c.get('/api/overview').status_code,200);self.assertEqual(c.post('/api/logout',json={},headers=headers).status_code,200);self.assertEqual(c.get('/api/overview').status_code,401)
