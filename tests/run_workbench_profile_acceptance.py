"""Opt-in owned Leo profile read through deployed HQ and workbench APIs."""
import json
from pathlib import Path
import time
import sys
import requests
from headquarters.autonomy import MulticaAdapter
from headquarters.business_executor import BusinessClient
ROOT=Path(__file__).resolve().parents[1]

def main():
    session=requests.Session();session.trust_env=False;base='http://127.0.0.1:8781';headers={'Origin':'https://d.58nft.net:9457','X-App-Request':'1'}
    response=session.post(base+'/api/login',json={'password':(ROOT/'.local/admin-password').read_text().strip()},headers=headers);response.raise_for_status()
    headers['Cookie']='; '.join(c.name+'='+c.value for c in session.cookies)
    def api(method,path,data=None):
        response=session.request(method,base+path,json=data,headers=headers,timeout=30);response.raise_for_status();return response.json()
    model='--model-summary' in sys.argv
    catalog=api('GET','/api/autonomy/capabilities')
    resource=next(r for r in catalog['resources'] if r['source_id']=='xhs' and r['account_id']=='1')
    assert next(o for o in resource['operations'] if o['id']=='profile.read')['available']
    payload={'title':'Leo真实主页只读闭环验收','objective':'Use business_task profile.read on the only granted Leo account first. Based only on its authenticated workbench result, create leo-profile.md with a concise factual profile summary including nickname leo and explicitly indicate authenticated profile as source. Unknown facts remain unknown. Do not access any other account or perform social writes. Then complete from verified artifact evidence.',
        'delivery_type':'local_artifact','completion_criteria':['真实主页来源摘要'],
        'artifact_checks':{'真实主页来源摘要':{'name':'leo-profile.md','contains':['leo','authenticated profile']}},
        'grants':[{'resource_id':resource['id'],'operations':['profile.read'],'scope':{}}], 'limits':{'max_actions':8}}
    if model:
        config=json.loads((ROOT/'.local/autonomy.json').read_text());remote=MulticaAdapter(config['multica_config_file']);agent=next(x for x in remote.request('GET','/api/agents') if x['name']=='小红书-leo')
        previous=json.loads((ROOT/'docs/workbench-profile-live-acceptance.json').read_text());profile=previous['business_tasks'][0]['result']['data']
        payload.update(title='Leo生产模型任务正文只读验收',source_material=json.dumps({'source':'authenticated_platform_sdk','coverage':'current_authenticated_profile','profile':profile},ensure_ascii=False),objective='First read_brief to observe the supplied actual owned Leo profile. Then business_task content.analyze for the sole granted Leo account with input agent_id '+agent['id']+' and text containing only these actual profile facts. In the text explicitly request a concise factual Chinese profile summary; permit only reading this assigned Multica issue and its triggering context, forbid social writes, other account access, persona or memory modifications, and forbid any generated fictional experiences. After actual model result is returned, create leo-profile.md citing the brief observation and including nickname leo and authenticated profile; incorporate only usable factual model observations and mark unknowns unknown. Complete after artifact verification. Do not call other operations.',grants=[{'resource_id':resource['id'],'operations':['content.analyze'],'scope':{}}])
    goal=api('POST','/api/autonomy',payload)
    print('Owned profile model-summary goal created' if model else 'Owned profile goal created with only profile.read',flush=True);start=time.time();last=''
    while time.time()-start<800:
        result=api('GET','/api/autonomy/'+goal['id']);state=result['status']+':'+str(len(result['actions']))+':'+result['summary']
        if state!=last:print(state,flush=True);last=state
        if result['status']!='active':break
        time.sleep(3)
    config=json.loads((ROOT/'.local/autonomy.json').read_text());multica=MulticaAdapter(config['multica_config_file']);workbench=BusinessClient(config['workbenches']['xhs'])
    report={'scope':('Production Leo model-context read-only verification from actual profile facts; no social or memory writes' if model else 'Actual owned Leo profile; deployed HQ/native Multica/XHS business API; no social writes'),'goal':result,'business_tasks':[]}
    for action in result['actions']:
        if action.get('result',{}).get('business_task_id'):
            report['business_tasks'].append(workbench.request('GET','/api/business/tasks/'+action['result']['business_task_id']))
    if result.get('project_id'):
        project=multica.request('GET','/api/projects/'+result['project_id']);main=multica.request('GET','/api/issues/'+result['main_issue_id']);report['project_readback']={'project_id':project['id'],'main_issue_project_id':main['project_id']}
    report['four_account_catalog']=[{'name':r['name'],'platform':r['source_id'],'account_id':r['account_id'],'operations':r['operations']} for r in catalog['resources']]
    (ROOT/('docs/workbench-production-model-acceptance.json' if model else 'docs/workbench-profile-live-acceptance.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2))
    if model:
        report['usable_model_result']=any(t.get('state',t.get('status'))=='succeeded' and (t.get('result') or {}).get('source')=='model_intermediate' for t in report['business_tasks'])
        (ROOT/'docs/workbench-production-model-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('Saved deployed read-only goal acceptance report',flush=True)
    if result['status']!='completed' or (model and not report['usable_model_result']):raise SystemExit('Actual profile goal did not complete')
if __name__=='__main__':main()
