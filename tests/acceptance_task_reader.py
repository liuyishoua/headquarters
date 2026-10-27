"""Test-only MCP reader: assigned synthetic Channel issue body, no other operations."""
import json
import os
import re
import sys
from urllib.parse import urlsplit
import requests

TOOL={'name':'read_assigned_analysis','description':'Read the assigned synthetic Channel analysis issue body. No platform or memory operations.','inputSchema':{'type':'object','properties':{'issue_id':{'type':'string'}},'required':['issue_id'],'additionalProperties':False}}

def reply(call):
    method=call.get('method');params=call.get('params') or {}
    if method=='initialize':return {'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'synthetic-task-reader','version':'1'}}
    if method=='tools/list':return {'tools':[TOOL]}
    if method=='tools/call':
        try:
            if params.get('name')!=TOOL['name']:raise ValueError()
            issue=(params.get('arguments') or {}).get('issue_id','')
            if not re.fullmatch(r'[A-Za-z0-9-]{1,100}',issue):raise ValueError()
            base=os.environ['MULTICA_SERVER_URL'].rstrip('/');token=os.environ['MULTICA_TOKEN'];project=os.environ['ACCEPTANCE_PROJECT_ID'];u=urlsplit(base)
            if u.scheme!='https' and not (u.scheme=='http' and u.hostname in ('127.0.0.1','localhost')):raise ValueError()
            with requests.Session() as s:
                s.trust_env=False;r=s.get(base+'/api/issues/'+issue,headers={'Authorization':'Bearer '+token},timeout=15,allow_redirects=False)
            r.raise_for_status();data=r.json()
            if data.get('project_id')!=project or 'synthetic-business' not in str(data.get('title')) or '<!-- channel-batch:' not in str(data.get('description')):raise ValueError()
            body=json.dumps({'title':data['title'],'description':data.get('description','')[:50000]},ensure_ascii=False)
            return {'content':[{'type':'text','text':body}]}
        except Exception:return {'isError':True,'content':[{'type':'text','text':'Assigned synthetic task context could not be verified; do not draft or send.'}]}
    if method=='ping':return {}
    return None
for line in sys.stdin:
    try:
        call=json.loads(line)
        if 'id' not in call:continue
        value=reply(call)
        response={'jsonrpc':'2.0','id':call['id'],'result':value} if value is not None else {'jsonrpc':'2.0','id':call['id'],'error':{'code':-32601,'message':'Unsupported method'}}
        print(json.dumps(response,ensure_ascii=False),flush=True)
    except Exception:pass
