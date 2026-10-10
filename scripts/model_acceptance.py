"""Real Multica with temporary Headquarters and Channel; no social execution."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid
import requests
from waitress import create_server
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT.parent/'channel')]
from channel_service.app import create_app as channel_app
from channel_service.multica import Multica
from headquarters.app import create_app
from headquarters.client import ChannelClient


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true');args=parser.parse_args()
    if not args.execute:parser.error('Use --execute for authorized synthetic model acceptance')
    turns=[];server=None;hq=None;mission=None;app=None
    with tempfile.TemporaryDirectory(prefix='headquarters-real-model-') as directory:
        try:
            data=Path(directory);adapter=Multica(ROOT.parent/'channel/.local/multica.json')
            agents=adapter.agents();agent=next((a for a in agents if '回复' in a['name']),agents[0])
            app=channel_app(data/'channel',adapter,auth=False,background=True)
            source=app.extensions['channel'].source('xianyu');auto=app.extensions['automation']
            account='synthetic-hq-model-acceptance'
            auto.heartbeat('xianyu',dict(account_id=account,executor_id='synthetic-observer-no-claims',capabilities=['dm.reply']))
            server=create_server(app,host='127.0.0.1',port=18874);threading.Thread(target=server.run,daemon=True).start()
            client=ChannelClient(dict(base_url='http://127.0.0.1:18874',token_file=str(data/'channel/command-key')))
            hq=create_app(data/'hq',{'synthetic-channel':client},auth=False).extensions['headquarters'];hq.tick()
            resource=hq.save_account(dict(channel_id='synthetic-channel',source_id='xianyu',account_id=account,name='虚构模型验收账号',hour_start=0,hour_end=24))
            mission=hq.save_mission(dict(name='指挥部合成模型验收',objective='仅依据虚构资料解答客户咨询，给出简短私信方案，不执行平台发送。',knowledge='虚构商品99元，支持包邮，有蓝色和白色；具体发货时间未知。',persona='一句自然简洁的中文回答，一次只推进一个问题。',stages=[dict(id='consult',name='咨询处理',objective='回答包邮和颜色问题',criterion=dict(metric='outcome',kind='resolved',target=1))]))
            hq.assign(mission['id'],dict(stage_id='consult',account_id=resource['id'],agent_id=agent['id'],mode='review',role='依据事实答疑，不编造发货承诺。'))
            hq.transition(mission['id'],'start');hq.tick()
            for body in ['这个虚构商品99元，可以包邮吗？','我想选蓝色，可以选吗？']:
                response=requests.post('http://127.0.0.1:18874/v1/events',headers={'Authorization':'Bearer '+source['token']},json=dict(account_id=account,event_id='synthetic-'+str(uuid.uuid4()),event_type='dm.received',conversation_id='synthetic-hq-customer',content={'text':body}),timeout=15)
                response.raise_for_status();event=response.json();deadline=time.time()+240
                while time.time()<deadline:
                    hq.tick();latest=hq.mission(mission['id']);action=next((a for a in latest['actions'] if a['event_id']==event['id']),None)
                    current=next(x for x in auto.events() if x['id']==event['id']);job=current['jobs'][0]
                    if job['state']=='needs_human':raise RuntimeError(job['error'])
                    if action:
                        assert action['state']=='review'
                        message=app.extensions['channel'].message(action['channel_event'])
                        assert message['payload']['context']['command']['mission_id']==mission['id']
                        turns.append(dict(event_id=event['id'],action_id=action['id'],goal_id=action['goal_id'],case_id=action['case_id'],channel_event=action['channel_event'],issue_id=message['execution']['issue_id'],task_id=message['execution']['task_id'],proposal=action['payload'],state=action['state']))
                        break
                    time.sleep(2)
                else:raise RuntimeError('Real model acceptance timeout')
            assert len({x['issue_id'] for x in turns})==1
            assert len({x['task_id'] for x in turns})==2
            assert '包邮' in turns[0]['proposal']['text'] and '蓝' in turns[1]['proposal']['text']
            assert hq.mission(mission['id'])['progress']['consult']==0
            hq.transition(mission['id'],'pause');hq.tick()
            assert not any(g['enabled'] for g in auto.goals())
            report=dict(passed=True,real_model_calls=2,real_platform_calls=0,mission_id=mission['id'],agent_id=agent['id'],mission_paused_after_test=True,business_progress=0,turns=turns)
            (ROOT/'docs/model-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False,indent=2))
        finally:
            if hq and mission:
                try:hq.transition(mission['id'],'pause');hq.tick()
                except Exception:pass
            if app:app.extensions['worker'].stop()
            if server:server.close()

if __name__=='__main__':main()
