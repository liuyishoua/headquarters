"""Opt-in live model acceptance. Creates only planning/audit issues, no social actions."""
import json
from pathlib import Path
import tempfile
import time
from headquarters.autonomy import AutonomyStore, MulticaAdapter, MulticaPlanner


def main():
    config=json.loads(Path('.local/autonomy.json').read_text())
    remote=MulticaAdapter(config['multica_config_file'])
    report={'scope':'Native Multica planning; synthetic briefs; local artifacts only','domains':[]}
    with tempfile.TemporaryDirectory(prefix='hq-model-acceptance-') as directory:
        for domain, material, checks in [
            ('market','Survey evidence: buyers value flexible pickup more than low price. The previous strategy stressed discounts. Revise the recommendation using this observation.',['flexible pickup','discount']),
            ('research','Benchmark evidence: approach B had fewer errors but longer runtime than A. The previous strategy preferred fastest runtime. Revise recommendation using this observation.',['errors','runtime'])]:
            store=AutonomyStore(Path(directory)/domain,planner=MulticaPlanner(remote,config['planner_agent_id'],timeout=240),multica=remote)
            run=store.create({'title':'Synthetic '+domain+' model acceptance','objective':
                'Read the supplied brief using read_brief first. Then create '+domain+'.md, an evidence-backed English report adjusting the previous strategy based on supplied observations. Cite the brief observation ID. Finally complete with artifact evidence. Do not perform any external action.',
                'source_material':material,'delivery_type':'local_artifact','completion_criteria':['Evidence-backed report'],
                'artifact_checks':{'Evidence-backed report':{'name':domain+'.md','contains':checks}},'limits':{'max_actions':8}})
            print(domain+': real model goal created',flush=True)
            start=time.time();last=''
            while time.time()-start<600:
                result=store.tick(run['id']) or store.get(run['id'])
                state=result['status']+':'+str(len(result['actions']))+':'+result['summary']
                if state!=last:
                    print(domain+': '+state,flush=True);last=state
                if result['status'] not in ('active',):break
                time.sleep(3)
            result=store.get(run['id'])
            entry={k:result[k] for k in ('id','status','project_id','main_issue_id','summary','blocker')}
            entry['domain']=domain
            entry['actions']=[{k:a[k] for k in ('kind','status','summary','arguments','result')} for a in result['actions']]
            entry['observations']=result['observations']
            report['domains'].append(entry)
            # Same store reopen and terminal tick must not create more model decisions/actions.
            count=len(result['actions']);reopened=AutonomyStore(Path(directory)/domain,planner=store.planner,multica=remote)
            before_writes=0
            with reopened.db() as db:before_writes=db.execute('SELECT count(*) FROM remote_writes').fetchone()[0]
            reopened.tick(run['id'])
            with reopened.db() as db:after_writes=db.execute('SELECT count(*) FROM remote_writes').fetchone()[0]
            entry['restart_no_duplicate']=len(reopened.get(run['id'])['actions'])==count and before_writes==after_writes
            entry['feedback_adaptation_verified']=any(a['kind']=='write_artifact' and all(x in a['arguments'].get('content','') for x in checks) for a in result['actions'])
            entry['artifact_checks_verified']=result['status']=='completed'
    path=Path('docs/autonomy-model-acceptance.json');path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    if not all(d['status']=='completed' and d['restart_no_duplicate'] and d['feedback_adaptation_verified'] for d in report['domains']):
        raise SystemExit('Live model acceptance incomplete; inspect saved report')
    print('Both domains completed with native model and verified artifacts',flush=True)


if __name__=='__main__':main()
