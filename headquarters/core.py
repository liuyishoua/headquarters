"""Goal-oriented control plane, durable projections and evidence ledger."""
import contextlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

ACTIONS = {'dm.reply', 'comment.reply', 'content.publish'}
EVENTS = {'dm.received', 'dm.followup_due', 'comment.received', 'content.requested'}
OUTCOMES = {'resolved', 'qualified', 'published', 'confirmed'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS missions(id TEXT PRIMARY KEY,version INTEGER NOT NULL,config TEXT NOT NULL,state TEXT NOT NULL,stage INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,channel_id TEXT NOT NULL,source_id TEXT NOT NULL,account_id TEXT NOT NULL,name TEXT NOT NULL,policy TEXT NOT NULL,UNIQUE(channel_id,source_id,account_id));
CREATE TABLE IF NOT EXISTS assignments(id TEXT PRIMARY KEY,mission_id TEXT NOT NULL,stage_id TEXT NOT NULL,account_id TEXT NOT NULL,config TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deployments(assignment_id TEXT PRIMARY KEY,channel_id TEXT NOT NULL,goal_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,applied INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',last_ack REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS channels(id TEXT PRIMARY KEY,snapshot TEXT NOT NULL DEFAULT '{}',cursor INTEGER NOT NULL DEFAULT 0,last_seen REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS actions(channel_id TEXT NOT NULL,id TEXT NOT NULL,assignment_id TEXT NOT NULL,mission_id TEXT NOT NULL,stage_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(channel_id,id));
CREATE TABLE IF NOT EXISTS outcomes(id TEXT PRIMARY KEY,mission_id TEXT NOT NULL,stage_id TEXT NOT NULL,kind TEXT NOT NULL,subject TEXT NOT NULL,channel_id TEXT NOT NULL,action_id TEXT NOT NULL,note TEXT NOT NULL,created REAL NOT NULL,UNIQUE(mission_id,stage_id,kind,subject));
CREATE TABLE IF NOT EXISTS transitions(mission_id TEXT NOT NULL,stage_id TEXT NOT NULL,completed REAL NOT NULL,evidence TEXT NOT NULL,PRIMARY KEY(mission_id,stage_id));
CREATE INDEX IF NOT EXISTS assignment_mission ON assignments(mission_id,stage_id);
CREATE INDEX IF NOT EXISTS action_mission ON actions(mission_id,stage_id);
CREATE INDEX IF NOT EXISTS deployment_channel ON deployments(channel_id);
CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,resource TEXT NOT NULL,detail TEXT NOT NULL,created REAL NOT NULL);
'''


def uid():
    return str(uuid.uuid4())


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def string(value, name, maximum=2000, required=True):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise Problem(f'{name}必须是最多 {maximum} 字的文字')
    value = value.strip()
    if required and not value:
        raise Problem(f'请填写{name}')
    return value


def number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise Problem(f'{name}须在 {low}–{high} 之间')
    return value


def choices(value, name, allowed):
    if not isinstance(value, list) or not value or any(not isinstance(x, str) for x in value) or not set(value) <= allowed:
        raise Problem(f'{name}无效')
    return sorted(set(value))


def policy(data):
    start = number(data.get('hour_start', 9), '开始小时', 0, 23)
    end = number(data.get('hour_end', 21), '结束小时', 1, 24)
    if start >= end:
        raise Problem('结束时间须晚于开始时间')
    return dict(daily_limit=number(data.get('daily_limit', 50), '账号每日额度', 1, 1000),
        min_interval=number(data.get('min_interval', 60), '账号动作间隔', 10, 86400),
        hour_start=start, hour_end=end)


def criterion(data):
    metric = data.get('metric', 'outcome')
    if metric not in ('outcome', 'receipt'):
        raise Problem('阶段完成标准无效')
    kind = data.get('kind', 'resolved' if metric == 'outcome' else 'dm.reply')
    if kind not in (OUTCOMES if metric == 'outcome' else ACTIONS):
        raise Problem('完成标准类别无效')
    return dict(metric=metric, kind=kind, target=number(data.get('target', 1), '阶段目标数量', 1, 100000))


class Headquarters:
    def __init__(self, directory, clients):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'headquarters.sqlite3'
        self.clients = clients
        self.lock = threading.RLock()
        self.tick_locks = {key: threading.Lock() for key in clients}
        self.stop_event = threading.Event()
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)
            for key in clients:
                db.execute('INSERT OR IGNORE INTO channels(id) VALUES(?)', (key,))
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def audit(self, db, kind, resource, detail=''):
        db.execute('INSERT INTO audit(kind,resource,detail,created) VALUES(?,?,?,?)', (kind, resource, detail, time.time()))

    def _mission(self, row):
        return dict(row, config=json.loads(row['config']))

    def save_mission(self, data, mission_id=None):
        stages = data.get('stages', [])
        if not isinstance(stages, list) or not 1 <= len(stages) <= 20:
            raise Problem('请配置 1–20 个有序阶段')
        normalized = []
        for stage in stages:
            if not isinstance(stage, dict):
                raise Problem('阶段必须是对象')
            normalized.append(dict(id=string(stage.get('id', uid()), '阶段标识', 80), name=string(stage.get('name'), '阶段名称', 80),
                objective=string(stage.get('objective', ''), '阶段要求', 2000, False), criterion=criterion(stage.get('criterion', {}))))
        if len({s['id'] for s in normalized}) != len(normalized):
            raise Problem('阶段标识不能重复')
        config = dict(name=string(data.get('name'), '目标名称', 80), objective=string(data.get('objective'), '业务目标'),
            knowledge=string(data.get('knowledge', ''), '共享业务知识', 5000, False),
            persona=string(data.get('persona', ''), '沟通风格', 1000, False), stages=normalized)
        with self.lock, self.db(True) as db:
            old = db.execute('SELECT * FROM missions WHERE id=?', (mission_id,)).fetchone() if mission_id else None
            if mission_id and not old:
                raise LookupError()
            # Once evidence exists, create a new mission instead of rewriting its meaning.
            if old and (old['state'] not in ('draft', 'paused') or old['stage'] or db.execute('SELECT 1 FROM actions WHERE mission_id=?', (mission_id,)).fetchone()):
                raise Problem('已有执行记录的目标不能重写，请暂停后调整账号分工或创建新目标', 409)
            if old:
                assigned = {r[0] for r in db.execute('SELECT stage_id FROM assignments WHERE mission_id=?', (mission_id,))}
                if not assigned <= {s['id'] for s in normalized}:
                    raise Problem('请保留已分配账号的阶段标识', 409)
            mission_id = mission_id or uid()
            now = time.time()
            db.execute('INSERT INTO missions VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,config=excluded.config,updated=excluded.updated',
                (mission_id, old['version'] + 1 if old else 1, encode(config), old['state'] if old else 'draft', old['stage'] if old else 0, old['created'] if old else now, now))
            self.audit(db, 'mission.saved', mission_id, config['name'])
            self._project(db, mission_id)
        return self.mission(mission_id)

    def save_account(self, data):
        channel = string(data.get('channel_id'), 'Channel', 80)
        if channel not in self.clients:
            raise Problem('Channel 未配置，请先由服务端接入')
        source = string(data.get('source_id'), '平台来源', 64)
        account = string(data.get('account_id'), '平台账号标识', 200)
        with self.lock, self.db(True) as db:
            catalog = json.loads(db.execute('SELECT snapshot FROM channels WHERE id=?', (channel,)).fetchone()[0])
            if source not in {x['id'] for x in catalog.get('sources', [])}:
                raise Problem('来源不在此 Channel 的实际目录中')
            if db.execute('SELECT 1 FROM accounts WHERE source_id=? AND account_id=? AND channel_id<>?', (source, account, channel)).fetchone():
                raise Problem('同一平台账号只能选择一个 Channel 执行，避免重复控制', 409)
            old = db.execute('SELECT * FROM accounts WHERE channel_id=? AND source_id=? AND account_id=?', (channel, source, account)).fetchone()
            account_id = old['id'] if old else uid()
            db.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,policy=excluded.policy',
                (account_id, channel, source, account, string(data.get('name', account), '账号名称', 80), encode(policy(data))))
            self.audit(db, 'account.saved', account_id)
            for row in db.execute('SELECT DISTINCT mission_id FROM assignments WHERE account_id=?', (account_id,)).fetchall():
                self._project(db, row[0])
        return {'id': account_id}

    def assign(self, mission_id, data):
        with self.lock, self.db(True) as db:
            row = db.execute('SELECT * FROM missions WHERE id=?', (mission_id,)).fetchone()
            if not row:
                raise LookupError()
            mission = self._mission(row)
            if mission['state'] not in ('draft', 'paused'):
                raise Problem('请先暂停目标，再调整账号分工', 409)
            stage_id = string(data.get('stage_id'), '阶段', 80)
            if stage_id not in {s['id'] for s in mission['config']['stages']}:
                raise Problem('阶段不存在')
            account_id = string(data.get('account_id'), '执行账号标识', 80)
            account = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
            if not account:
                raise Problem('执行账号尚未登记')
            config = dict(agent_id=string(data.get('agent_id'), 'Agent', 80),
                role=string(data.get('role', ''), '分工要求', 1500, False),
                event_types=choices(data.get('event_types', ['dm.received', 'dm.followup_due']), '触发事件', EVENTS),
                allowed_actions=choices(data.get('allowed_actions', ['dm.reply']), '允许动作', ACTIONS),
                mode=data.get('mode', 'review'), max_followups=number(data.get('max_followups', 2), '跟进上限', 0, 5))
            if config['mode'] not in ('review', 'automatic'):
                raise Problem('执行模式无效')
            map_actions = {'dm.reply': 'dm.received', 'comment.reply': 'comment.received', 'content.publish': 'content.requested'}
            if any(map_actions[a] not in config['event_types'] for a in config['allowed_actions']):
                raise Problem('允许动作须配置对应的业务触发事件')
            if any(not json.loads(x[0]).get('retired') for x in db.execute('SELECT config FROM assignments WHERE mission_id=? AND stage_id=? AND account_id=?', (mission_id, stage_id, account_id))):
                raise Problem('同一阶段的账号已分配；请移除旧分工后重新分配', 409)
            assignment_id = uid()
            db.execute('INSERT INTO assignments VALUES(?,?,?,?,?)', (assignment_id, mission_id, stage_id, account_id, encode(config)))
            self.audit(db, 'assignment.created', mission_id, assignment_id)
            self._project(db, mission_id)
        return {'id': assignment_id}

    def remove_assignment(self, assignment_id):
        with self.lock, self.db(True) as db:
            row = db.execute('SELECT a.*,m.state FROM assignments a JOIN missions m ON m.id=a.mission_id WHERE a.id=?', (assignment_id,)).fetchone()
            if not row:
                raise LookupError()
            if row['state'] not in ('draft', 'paused'):
                raise Problem('请先暂停目标', 409)
            deployment = db.execute('SELECT * FROM deployments WHERE assignment_id=?', (assignment_id,)).fetchone()
            if deployment and (json.loads(deployment['payload'])['goal']['enabled'] or deployment['applied'] != deployment['revision']):
                raise Problem('请等待暂停指令确认后再移除分工', 409)
            # Retain tombstones and receipts; old lower-layer leases must never be resurrected.
            config = json.loads(row['config']); config['retired'] = True
            db.execute('UPDATE assignments SET config=? WHERE id=?', (encode(config), assignment_id))
            self.audit(db, 'assignment.retired', row['mission_id'], assignment_id)
        return {'ok': True}

    def _assignments(self, db, mission_id=None):
        rows = db.execute('SELECT a.*,r.channel_id,r.source_id,r.account_id AS platform_account,r.policy,r.name AS account_name FROM assignments a JOIN accounts r ON r.id=a.account_id WHERE (? IS NULL OR a.mission_id=?)', (mission_id, mission_id))
        return [dict(r, config=json.loads(r['config']), policy=json.loads(r['policy'])) for r in rows]

    def _progress(self, db, mission_id, stage):
        criterion = stage['criterion']
        if criterion['metric'] == 'outcome':
            return db.execute('SELECT count(*) FROM outcomes WHERE mission_id=? AND stage_id=? AND kind=?', (mission_id, stage['id'], criterion['kind'])).fetchone()[0]
        rows = db.execute('SELECT payload FROM actions WHERE mission_id=? AND stage_id=?', (mission_id, stage['id']))
        return len({(a['source'], a['account'], a['receipt']['external_id']) for row in rows if (a := json.loads(row[0]))['state'] == 'succeeded' and a['type'] == criterion['kind'] and (a.get('receipt') or {}).get('external_id')})

    def _project(self, db, mission_id):
        mission = self._mission(db.execute('SELECT * FROM missions WHERE id=?', (mission_id,)).fetchone())
        stages = mission['config']['stages']
        current = stages[min(mission['stage'], len(stages) - 1)]
        for assignment in self._assignments(db, mission_id):
            stage = next(s for s in stages if s['id'] == assignment['stage_id'])
            config = assignment['config']
            active = mission['state'] == 'active' and current['id'] == stage['id'] and not config.get('retired')
            context = dict(mission_id=mission_id, mission_name=mission['config']['name'], mission_objective=mission['config']['objective'],
                stage_id=stage['id'], stage_name=stage['name'], success_criterion=stage['criterion'],
                assignment_id=assignment['id'], role=config['role'],
                previous_stage_results=[dict(subject=r['subject'], kind=r['kind'], note=r['note'][:300]) for r in db.execute('SELECT subject,kind,note FROM outcomes WHERE mission_id=? AND stage_id IN (SELECT stage_id FROM transitions WHERE mission_id=?) ORDER BY created DESC LIMIT 10', (mission_id, mission_id))])
            goal = dict(source_id=assignment['source_id'], account_id=assignment['platform_account'],
                name=(mission['config']['name'] + ' · ' + stage['name'])[:80],
                objective=(mission['config']['objective'] + '\n本阶段：' + stage['objective'] + '\n账号分工：' + config['role'])[:2000],
                knowledge=mission['config']['knowledge'], persona=mission['config']['persona'],
                agent_id=config['agent_id'], event_types=config['event_types'], allowed_actions=config['allowed_actions'],
                mode=config['mode'], enabled=active, max_followups=config['max_followups'], **assignment['policy'])
            previous = db.execute('SELECT * FROM deployments WHERE assignment_id=?', (assignment['id'],)).fetchone()
            payload = dict(goal=goal, context=context, mission_id=mission_id, assignment_id=assignment['id'], lease_seconds=90)
            if previous:
                old_payload = json.loads(previous['payload']); old_payload.pop('revision')
                if old_payload == payload:
                    continue
            revision = previous['revision'] + 1 if previous else 1
            payload['revision'] = revision
            goal_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'headquarters:' + assignment['id']))
            db.execute('INSERT INTO deployments(assignment_id,channel_id,goal_id,revision,payload) VALUES(?,?,?,?,?) ON CONFLICT(assignment_id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload,error=\'\'',
                (assignment['id'], assignment['channel_id'], goal_id, revision, encode(payload)))

    def _blockers(self, db, mission):
        stage = mission['config']['stages'][mission['stage']]
        selected = [a for a in self._assignments(db, mission['id']) if a['stage_id'] == stage['id'] and not a['config'].get('retired')]
        if not selected:
            return ['当前阶段尚未分配执行账号']
        reasons = []
        for a in selected:
            channel = db.execute('SELECT * FROM channels WHERE id=?', (a['channel_id'],)).fetchone()
            snapshot = json.loads(channel['snapshot'])
            label = a['account_name']
            if channel['last_seen'] < time.time() - 30 or channel['error']:
                reasons.append(label + '：Channel 未连接或状态过期'); continue
            if a['source_id'] not in {s['id'] for s in snapshot.get('sources', []) if s['enabled']}:
                reasons.append(label + '：平台来源已停用')
            connector = next((c for c in snapshot.get('connectors', []) if c['source'] == a['source_id'] and c['account'] == a['platform_account']), None)
            if not connector or connector['last_seen'] < time.time() - 120:
                reasons.append(label + '：没有在线执行器')
            elif not set(a['config']['allowed_actions']) <= set(connector['capabilities']):
                reasons.append(label + '：执行器不支持所需动作')
            if a['config']['agent_id'] not in {x['id'] for x in snapshot.get('agents', [])}:
                reasons.append(label + '：Agent 不在该 Channel 的可用目录中')
            for b in self._assignments(db):
                if b['id'] == a['id'] or b['config'].get('retired') or b['account_id'] != a['account_id']:
                    continue
                other = self._mission(db.execute('SELECT * FROM missions WHERE id=?', (b['mission_id'],)).fetchone())
                other_active = (other['id'] == mission['id'] and b['stage_id'] == stage['id']) or (other['state'] == 'active' and b['stage_id'] == other['config']['stages'][other['stage']]['id'])
                if other_active and set(b['config']['event_types']) & set(a['config']['event_types']):
                    reasons.append(label + '：与目标「' + other['config']['name'] + '」争用同类事件，请先暂停或调整分工')
            for legacy in snapshot.get('unmanaged_goals', []):
                if legacy['enabled'] and legacy['source_id'] == a['source_id'] and legacy['account_id'] == a['platform_account'] and set(legacy['event_types']) & set(a['config']['event_types']):
                    reasons.append(label + '：与 Channel 本地规则「' + legacy['name'] + '」冲突')
        return sorted(set(reasons))

    def transition(self, mission_id, operation):
        with self.lock, self.db(True) as db:
            row = db.execute('SELECT * FROM missions WHERE id=?', (mission_id,)).fetchone()
            if not row:
                raise LookupError()
            mission = self._mission(row)
            if operation == 'start':
                if mission['state'] not in ('draft', 'paused'):
                    raise Problem('只有草稿或暂停的目标可以启动', 409)
                reasons = self._blockers(db, mission)
                if reasons:
                    raise Problem('；'.join(reasons), 409)
                db.execute("UPDATE missions SET state='active',version=version+1,updated=? WHERE id=?", (time.time(), mission_id))
            elif operation == 'pause':
                if mission['state'] == 'completed':
                    raise Problem('目标已经完成', 409)
                db.execute("UPDATE missions SET state='paused',version=version+1,updated=? WHERE id=?", (time.time(), mission_id))
            elif operation == 'advance':
                if mission['state'] != 'active':
                    raise Problem('只有运行中的目标可以验收阶段', 409)
                stage = mission['config']['stages'][mission['stage']]
                if db.execute("SELECT 1 FROM actions WHERE mission_id=? AND json_extract(payload,'$.state') IN ('claimed','uncertain')", (mission_id,)).fetchone():
                    raise Problem('仍有执行中或待核对的动作，请先取得明确回执', 409)
                progress = self._progress(db, mission_id, stage)
                if progress < stage['criterion']['target']:
                    raise Problem('当前阶段尚未达到有证据的完成标准', 409)
                db.execute('INSERT INTO transitions VALUES(?,?,?,?)', (mission_id, stage['id'], time.time(), encode(dict(progress=progress, criterion=stage['criterion']))))
                if mission['stage'] == len(mission['config']['stages']) - 1:
                    db.execute("UPDATE missions SET state='completed',version=version+1,updated=? WHERE id=?", (time.time(), mission_id))
                else:
                    # Next stage needs an explicit start after prior instructions are withdrawn.
                    db.execute("UPDATE missions SET state='paused',stage=stage+1,version=version+1,updated=? WHERE id=?", (time.time(), mission_id))
            else:
                raise Problem('目标操作无效')
            self._project(db, mission_id)
            self.audit(db, 'mission.' + operation, mission_id)
        return self.mission(mission_id)

    def pause_all(self):
        with self.lock, self.db(True) as db:
            ids = [x[0] for x in db.execute("SELECT id FROM missions WHERE state='active'")]
            for mission_id in ids:
                db.execute("UPDATE missions SET state='paused',version=version+1,updated=? WHERE id=?", (time.time(), mission_id))
                self._project(db, mission_id)
                self.audit(db, 'mission.pause', mission_id, '集中暂停全部运行目标')
        return dict(paused=len(ids))

    def outcome(self, mission_id, data):
        action_id = string(data.get('action_id'), '回执动作标识', 80)
        channel_id = string(data.get('channel_id'), '回执 Channel', 80)
        kind = data.get('kind')
        if kind not in OUTCOMES:
            raise Problem('业务结果类别无效')
        # Subject is an explicit business key. Different platform nicknames are not joined automatically.
        subject = string(data.get('subject'), '业务对象标识', 200)
        note = string(data.get('note'), '验收说明', 1500)
        with self.lock, self.db(True) as db:
            mission = db.execute('SELECT * FROM missions WHERE id=?', (mission_id,)).fetchone()
            if not mission:
                raise LookupError()
            if mission['state'] == 'completed':
                raise Problem('已完成目标不能追加结果', 409)
            evidence = db.execute('SELECT * FROM actions WHERE channel_id=? AND id=? AND mission_id=?', (channel_id, action_id, mission_id)).fetchone()
            if not evidence:
                raise Problem('请引用本目标的已执行动作回执', 409)
            action = json.loads(evidence['payload'])
            if action['state'] != 'succeeded' or not (action.get('receipt') or {}).get('external_id'):
                raise Problem('模型建议和待执行动作不能作为完成证据', 409)
            config = json.loads(mission['config'])
            if evidence['stage_id'] != config['stages'][mission['stage']]['id']:
                raise Problem('只能为当前阶段登记业务结果', 409)
            old = db.execute('SELECT * FROM outcomes WHERE mission_id=? AND stage_id=? AND kind=? AND subject=?', (mission_id, evidence['stage_id'], kind, subject)).fetchone()
            if old:
                if any(old[k] != v for k, v in dict(channel_id=channel_id, action_id=action_id, note=note).items()):
                    raise Problem('同一业务结果已登记且内容不同', 409)
                return {'id': old['id'], 'duplicate': True}
            # One platform evidence cannot manufacture an arbitrary number of customers.
            if db.execute('SELECT 1 FROM outcomes WHERE mission_id=? AND stage_id=? AND kind=? AND channel_id=? AND action_id=?', (mission_id, evidence['stage_id'], kind, channel_id, action_id)).fetchone():
                raise Problem('此动作已用于同类业务结果，请引用新的完成证据', 409)
            for prior in db.execute('SELECT a.payload FROM outcomes o JOIN actions a ON a.channel_id=o.channel_id AND a.id=o.action_id WHERE o.mission_id=? AND o.stage_id=? AND o.kind=?', (mission_id, evidence['stage_id'], kind)):
                other = json.loads(prior[0])
                if (other['source'], other['account'], other['receipt']['external_id']) == (action['source'], action['account'], action['receipt']['external_id']):
                    raise Problem('同一平台回执不能重复记为多个业务对象', 409)
            outcome_id = uid()
            db.execute('INSERT INTO outcomes VALUES(?,?,?,?,?,?,?,?,?)', (outcome_id, mission_id, evidence['stage_id'], kind, subject, channel_id, action_id, note, time.time()))
            self.audit(db, 'outcome.confirmed', mission_id, outcome_id)
        return {'id': outcome_id, 'duplicate': False}

    def mission(self, mission_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM missions WHERE id=?', (mission_id,)).fetchone()
            if not row:
                raise LookupError()
            value = self._mission(row)
            value['assignments'] = self._assignments(db, mission_id)
            value['progress'] = {s['id']: self._progress(db, mission_id, s) for s in value['config']['stages']}
            value['blockers'] = self._blockers(db, value) if value['state'] != 'completed' else []
            value['deployments'] = [dict(x, payload=json.loads(x['payload'])) for x in db.execute('SELECT d.* FROM deployments d JOIN assignments a ON a.id=d.assignment_id WHERE a.mission_id=?', (mission_id,))]
            goal_ids = {x['goal_id'] for x in value['deployments']}
            value['cases'] = [dict(c, channel_id=ch['id']) for ch in db.execute('SELECT id,snapshot FROM channels') for c in json.loads(ch['snapshot']).get('cases', []) if c['goal_id'] in goal_ids]
            value['stats'] = {r[0]: r[1] for r in db.execute("SELECT json_extract(payload,'$.state'),count(*) FROM actions WHERE mission_id=? GROUP BY 1", (mission_id,))}
            value['outcomes'] = [dict(x) for x in db.execute('SELECT * FROM outcomes WHERE mission_id=? ORDER BY created DESC', (mission_id,))]
            value['actions'] = [dict(json.loads(x['payload']), channel_id=x['channel_id'], stage_id=x['stage_id']) for x in db.execute('SELECT * FROM actions WHERE mission_id=? ORDER BY rowid DESC LIMIT 200', (mission_id,))]
            return value

    def overview(self):
        with self.db() as db:
            ids = [x[0] for x in db.execute('SELECT id FROM missions ORDER BY updated DESC')]
            accounts = [dict(x, policy=json.loads(x['policy'])) for x in db.execute('SELECT * FROM accounts')]
            channels = [dict(x, snapshot=json.loads(x['snapshot'])) for x in db.execute('SELECT * FROM channels')]
            audit = [dict(x) for x in db.execute('SELECT * FROM audit ORDER BY seq DESC LIMIT 100')]
            metrics = dict(review=db.execute("SELECT count(*) FROM actions WHERE json_extract(payload,'$.state')='review'").fetchone()[0], outcomes=db.execute('SELECT count(*) FROM outcomes').fetchone()[0])
        return dict(missions=[self.mission(x) for x in ids], accounts=accounts, channels=channels, audit=audit, metrics=metrics)

    def decide_action(self, channel_id, action_id, approve):
        if not isinstance(approve, bool):
            raise Problem('approve 必须是布尔值')
        with self.lock, self.db() as db:
            action = db.execute('SELECT a.*,m.state FROM actions a JOIN missions m ON m.id=a.mission_id WHERE a.channel_id=? AND a.id=?', (channel_id, action_id)).fetchone()
            if not action or action['state'] != 'active':
                raise Problem('目标未运行或动作不存在', 409)
            payload = json.loads(action['payload'])
            if payload['state'] != 'review':
                raise Problem('此动作不在待审核状态', 409)
        self.clients[channel_id].decision(action_id, approve)
        with self.db(True) as db:
            self.audit(db, 'action.approve' if approve else 'action.reject', action['mission_id'], action_id)
        return {'ok': True}

    def tick(self, only_channel=None):
        for channel_id, client in self.clients.items():
            if only_channel and channel_id != only_channel:
                continue
            lock = self.tick_locks[channel_id]
            if not lock.acquire(blocking=False):
                continue
            try:
                self._dispatch(channel_id)
                self._sync(channel_id, client)
            finally:
                lock.release()

    def _sync(self, channel_id, client):
        try:
            catalog = client.catalog()
            for _ in range(10):
                with self.db() as db:
                    cursor = db.execute('SELECT cursor FROM channels WHERE id=?', (channel_id,)).fetchone()[0]
                state = client.state(cursor)
                with self.lock, self.db(True) as db:
                    for action in state['actions']:
                        dep = db.execute('SELECT a.* FROM assignments a JOIN deployments d ON d.assignment_id=a.id WHERE d.channel_id=? AND d.goal_id=?', (channel_id, action['goal_id'])).fetchone()
                        if dep:
                            db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?) ON CONFLICT(channel_id,id) DO UPDATE SET payload=excluded.payload', (channel_id, action['id'], dep['id'], dep['mission_id'], dep['stage_id'], encode(action)))
                    snapshot = dict(catalog, unmanaged_goals=state.get('unmanaged_goals', []), stats=state['stats'], cases=state['cases'])
                    db.execute('UPDATE channels SET snapshot=?,cursor=?,last_seen=?,error=\'\' WHERE id=?', (encode(snapshot), state['next_cursor'], time.time(), channel_id))
                if not state.get('has_more'):
                    break
        except Exception:
            with self.db(True) as db:
                db.execute('UPDATE channels SET error=? WHERE id=?', ('Channel 同步失败，请检查服务连接', channel_id))

    def _dispatch(self, channel_id):
        with self.db() as db:
            rows = [dict(r, payload=json.loads(r['payload'])) for r in db.execute('SELECT * FROM deployments WHERE channel_id=?', (channel_id,))]
        rows.sort(key=lambda r: (r['payload']['goal']['enabled'], r['last_ack']))
        renewals = [r for r in rows if r['payload']['goal']['enabled'] and r['applied'] == r['revision'] and r['last_ack'] <= time.time() - 25]
        # Renew hundreds of existing account grants in one HTTP round trip.
        for offset in range(0, len(renewals), 500):
            batch = renewals[offset:offset+500]
            try:
                accepted = set(self.clients[channel_id].renew([dict(goal_id=r['goal_id'], revision=r['revision']) for r in batch])['renewed'])
                with self.db(True) as db:
                    for row in batch:
                        if row['goal_id'] in accepted:
                            db.execute('UPDATE deployments SET last_ack=?,error=\'\' WHERE assignment_id=? AND revision=?', (time.time(), row['assignment_id'], row['revision']))
                        else:
                            db.execute('UPDATE deployments SET error=? WHERE assignment_id=? AND revision=?', ('下层版本不一致，请核对指令；授权不会被延长', row['assignment_id'], row['revision']))
            except Exception:
                with self.db(True) as db:
                    for row in batch:
                        db.execute('UPDATE deployments SET error=? WHERE assignment_id=? AND revision=?', ('续约未确认；既有授权最多 90 秒到期', row['assignment_id'], row['revision']))

        deadline = time.monotonic() + 15
        for row in rows:
            enabled = row['payload']['goal']['enabled']
            if row['applied'] == row['revision']:
                continue
            if time.monotonic() >= deadline:
                break
            with self.db() as db:
                fresh = db.execute('SELECT * FROM deployments WHERE assignment_id=?', (row['assignment_id'],)).fetchone()
                if fresh['revision'] != row['revision']:
                    continue
                if enabled:
                    current = db.execute('SELECT mission_id,account_id FROM assignments WHERE id=?', (row['assignment_id'],)).fetchone()
                    pending_stop = any(not json.loads(x['payload'])['goal']['enabled'] for x in db.execute('SELECT d.payload FROM deployments d JOIN assignments a ON a.id=d.assignment_id WHERE d.applied<>d.revision AND (a.mission_id=? OR a.account_id=?)', (current['mission_id'], current['account_id'])))
                    if pending_stop:
                        continue
            try:
                self.clients[channel_id].deploy(row['goal_id'], row['payload'])
                with self.db(True) as db:
                    db.execute('UPDATE deployments SET applied=?,last_ack=?,error=\'\' WHERE assignment_id=? AND revision=?', (row['revision'], time.time(), row['assignment_id'], row['revision']))
            except Exception:
                with self.db(True) as db:
                    db.execute('UPDATE deployments SET error=? WHERE assignment_id=? AND revision=?', ('下发未确认；保留指令重试，既有授权最多 90 秒到期', row['assignment_id'], row['revision']))

    def start_worker(self):
        # Each Channel has its own reconciler, so an unavailable Channel cannot
        # block leases or result collection on another.
        def run(channel_id):
            while not self.stop_event.is_set():
                try:
                    self.tick(channel_id)
                except Exception:
                    pass
                self.stop_event.wait(5)
        for channel_id in self.clients:
            threading.Thread(target=run, args=(channel_id,), daemon=True, name='reconcile-' + channel_id).start()
