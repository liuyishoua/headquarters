"""Temporary real HTTP + desktop/mobile browser flow; no real platform calls."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import requests
from playwright.sync_api import sync_playwright
from waitress import create_server

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent / 'channel'), str(ROOT / 'tests')]
from test_command import Model
from channel_service.app import create_app as channel_app
from headquarters.app import create_app
from headquarters.client import ChannelClient


def main():
    servers = []; apps = {}; keys = {}; errors = []
    out = ROOT / 'docs/previews'; out.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='headquarters-browser-') as directory:
        try:
            clients = {}
            for key, port, source in [('east', 18871, 'xianyu'), ('west', 18872, 'weibo')]:
                data = Path(directory) / key
                app = channel_app(data, Model(), auth=False, demo=True)
                keys[key] = app.extensions['channel'].source(source)['token']
                app.extensions['automation'].heartbeat(source, dict(account_id='synthetic-'+key, executor_id='qa-executor', capabilities=['dm.reply']))
                server = create_server(app, host='127.0.0.1', port=port); servers.append(server)
                threading.Thread(target=server.run, daemon=True).start(); apps[key] = app
                clients[key] = ChannelClient(dict(base_url='http://127.0.0.1:'+str(port), token_file=str(data/'command-key')))
            app = create_app(Path(directory)/'hq', clients, auth=False)
            hq = app.extensions['headquarters']; hq.tick()
            server = create_server(app, host='127.0.0.1', port=18873); servers.append(server)
            threading.Thread(target=server.run, daemon=True).start()
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width':1440,'height':1100})
                page.on('pageerror', lambda e:errors.append(str(e)))
                page.goto('http://127.0.0.1:18873/'); page.get_by_role('heading', name='先确定想完成什么。').wait_for()
                # Register resources through the actual UI.
                page.locator('[data-tab=accounts]').click()
                for key, source in [('east','xianyu'),('west','weibo')]:
                    page.get_by_role('button', name='登记执行账号 ＋').click()
                    form = page.locator('#account-form'); form.locator('[name=channel_id]').select_option(key)
                    form.locator('[name=name]').fill('合成账号-'+key); form.locator('[name=account_id]').fill('synthetic-'+key)
                    form.locator('[name=hour_start]').fill('0'); form.locator('[name=hour_end]').fill('24')
                    form.get_by_role('button', name='保存执行账号').click(); page.locator('#modal').wait_for(state='hidden')
                page.get_by_role('button', name='新建目标 ＋').click()
                form = page.locator('#mission-form'); form.locator('[name=name]').fill('多平台客服与跟进 · 合成验收')
                form.locator('[name=objective]').fill('协调两个虚构平台账号处理咨询，验收客户需求后进入跟进阶段。')
                form.locator('[name=knowledge]').fill('虚构商品支持包邮；不得承诺未提供的优惠。')
                first = form.locator('.stage-form').first
                first.locator('[name=stage_name]').fill('解决咨询'); first.locator('[name=stage_objective]').fill('完成两个虚构客户的咨询，并人工确认。'); first.locator('[name=target]').fill('2')
                form.get_by_role('button', name='添加下一阶段 ＋').click()
                second = form.locator('.stage-form').nth(1); second.locator('[name=stage_name]').fill('确认跟进'); second.locator('[name=stage_objective]').fill('确认一个虚构客户的后续需求。'); second.locator('[name=criterion]').select_option('outcome:qualified')
                form.get_by_role('button', name='创建目标', exact=True).click(); page.locator('#modal').wait_for(state='hidden')
                mission = hq.overview()['missions'][0]
                # Assign two Channels to one stage, and a separate follow-up stage.
                for stage_index, key in [(0,'east'),(0,'west'),(1,'east')]:
                    page.get_by_role('button', name='分配执行账号 ＋').click()
                    form = page.locator('#assignment-form'); form.locator('[name=stage_id]').select_option(mission['config']['stages'][stage_index]['id'])
                    resource = next(a for a in hq.overview()['accounts'] if a['channel_id']==key)
                    form.locator('[name=account_id]').select_option(resource['id']); form.locator('[name=role]').fill('虚构客服' if stage_index==0 else '虚构跟进专员')
                    form.get_by_role('button', name='保存分工').click(); page.locator('#modal').wait_for(state='hidden')
                hq.tick(); page.locator('#refresh').click()
                page.get_by_role('button', name='启动当前阶段').click()
                page.get_by_role('button', name='暂停目标').wait_for(); hq.tick()
                # Real HTTP inbound events and real queue processing, synthetic model only.
                for key, port, source in [('east',18871,'xianyu'),('west',18872,'weibo')]:
                    response=requests.post(f'http://127.0.0.1:{port}/v1/events',headers={'Authorization':'Bearer '+keys[key]},json=dict(account_id='synthetic-'+key,event_id='synthetic-incoming-'+key,event_type='dm.received',conversation_id='synthetic-customer-'+key,content={'text':'虚构商品可以包邮吗？'}))
                    assert response.status_code==202,response.text
                    auto,store,worker=(apps[key].extensions[x] for x in ('automation','channel','worker'))
                    for _ in range(8):
                        auto.tick()
                        with store.db(True) as db:db.execute('UPDATE batches SET next_poll=0,lease_until=0')
                        batch=store.claim()
                        if batch:worker.process(batch)
                    auto.tick()
                hq.tick(); page.locator('#refresh').click(); page.get_by_role('button', name='批准执行').first.wait_for()
                assert hq.mission(mission['id'])['progress'][mission['config']['stages'][0]['id']]==0
                for _ in range(2):
                    page.get_by_role('button',name='批准执行').first.click(); hq.tick(); page.locator('#refresh').click()
                for key,port,source in [('east',18871,'xianyu'),('west',18872,'weibo')]:
                    headers={'Authorization':'Bearer '+keys[key]}
                    response=requests.post(f'http://127.0.0.1:{port}/v1/actions/claim',headers=headers,json=dict(account_id='synthetic-'+key,executor_id='qa-executor'))
                    action=response.json()['action'];assert action
                    response=requests.post(f'http://127.0.0.1:{port}/v1/actions/'+action['id']+'/receipt',headers=headers,json=dict(account_id='synthetic-'+key,claim_token=action['claim_token'],status='succeeded',external_id='synthetic-platform-message-'+key))
                    assert response.status_code==200,response.text
                hq.tick();page.locator('#refresh').click()
                for key in ('east','west'):
                    page.get_by_role('button',name='登记业务结果').click();form=page.locator('#outcome-form')
                    form.locator('[name=subject]').fill('synthetic-customer-'+key)
                    action=next(a for a in hq.mission(mission['id'])['actions'] if a['channel_id']==key)
                    form.locator('[name=evidence]').select_option(key+'|'+action['id'])
                    form.locator('[name=note]').fill('人工核实：虚构客户已确认问题解决。')
                    form.get_by_role('button',name='确认并记账').click();page.locator('#modal').wait_for(state='hidden')
                page.screenshot(path=str(out/'headquarters-goal.png'),full_page=True)
                page.get_by_role('button',name='验收当前阶段').click();page.get_by_role('button',name='启动当前阶段').wait_for();hq.tick()
                m=hq.mission(mission['id']);assert m['stage']==1 and m['state']=='paused'
                assert not any(g['enabled'] for a in apps.values() for g in a.extensions['automation'].goals())
                for tab in ('accounts','channels','actions','audit','missions'):
                    page.locator('[data-tab='+tab+']').click();assert page.locator('[data-tab='+tab+'].selected').count()==1
                page.set_viewport_size({'width':390,'height':844})
                for tab in ('accounts','channels','actions','audit','missions'):
                    page.locator('[data-tab='+tab+']').click();assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),tab
                page.screenshot(path=str(out/'headquarters-mobile.png'),full_page=True)
                assert not errors,errors;browser.close()
            report=dict(passed=True,real_platform_calls=False,real_model_calls=False,channels=2,synthetic_accounts=2,
                stage_one_outcomes=2,next_stage_paused=True,old_rules_disabled=True,mobile_overflow=False,browser_errors=errors,
                flows=['UI resource registration','two-stage mission creation','cross-channel assignments','durable deployment over HTTP','source-authenticated event ingestion','agent proposal','HQ human adoption','account-scoped claim','authenticated platform receipt','evidence-backed business outcomes','stage acceptance and withdrawal','five tabs on desktop and mobile'])
            (ROOT/'docs/browser-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False,indent=2))
        finally:
            for server in servers:server.close()

if __name__=='__main__':main()
