"""Account-bound operation directory and read-only workbench adapter.

This adapter does not forward arbitrary paths or credentials to planners. Cached
workbench records are observations, never proof of a platform transaction.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import os
import subprocess
import threading
import time

OPERATIONS = {
    'profile.read': '读取账号自主页',
    'product.read': '读取自己的商品记录',
    'conversation.read': '读取已收到的会话',
    'message.read': '读取授权会话的消息',
    'content.read': '读取近期真实已发布作品',
    'public.search': '搜索公开需求',
    'draft.create': '创建内容草稿',
    'content.publish': '发布作品',
    'dm.reply': '回复私信',
    'comment.reply': '回复评论',
    'dm.initiate': '主动发起联系',
    'product.publish': '上架商品',
    'product.modify': '修改平台商品信息',
}


def resource_id(channel, source, account):
    return hashlib.sha256(json.dumps([channel, source, account]).encode()).hexdigest()[:24]


class CapabilityDirectory:
    def __init__(self, headquarters, api=None):
        self.hq = headquarters
        self.api = api
        self.cached = None
        self.expires = 0
        self.lock = threading.RLock()
        self.xhs_owner_id = None
        from .channel_executor import ChannelGoalExecutor
        self.channel_executor = ChannelGoalExecutor(headquarters.directory, headquarters.clients, self)

    def _api(self, project, path, account=None):
        if self.api is None:
            path_to_sdk = Path(__file__).resolve().parents[2] / 'agent-tools/leo-project-ops/scripts/leo_api.py'
            spec = importlib.util.spec_from_file_location('headquarters_ops_sdk', path_to_sdk)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.api = module.api
        result = self.api(project, path, account_id=account)
        if result.get('status') != 200:
            raise ValueError('账号工作台读取未确认')
        return result['data']

    def _xhs(self, operation, account=None):
        if not self.xhs_owner_id:
            raise ValueError('小红书工作台所属用户未配置')
        root = Path(__file__).resolve().parents[2]
        command = [str(root / 'xhs/.venv/bin/python'), str(root / 'headquarters/scripts/xhs_goal_read.py'),
                   operation, '--owner', str(self.xhs_owner_id)]
        if account is not None:
            command += ['--account', str(int(account))]
        env = dict(os.environ, CONFIG_FILE=str(root / 'xhs/.local/runtime.yaml'))
        completed = subprocess.run(command, cwd=root / 'xhs', env=env, capture_output=True, text=True, timeout=45)
        if completed.returncode:
            raise ValueError('小红书所选账号读取未确认')
        data = json.loads(completed.stdout)
        if not isinstance(data, dict) or data.get('error'):
            raise ValueError('小红书所选账号读取未确认')
        return data

    def __call__(self):
        with self.lock:
            if self.cached and self.expires > time.time():
                return self.cached
            overview = self.hq.overview()
            resources, agents = {}, {}
            for channel in overview['channels']:
                try:
                    live = self.hq.clients[channel['id']].catalog()
                except Exception:
                    live = {}
                for agent in channel['snapshot'].get('agents', []):
                    agents[agent['id']] = {k: agent.get(k) for k in ('id', 'name')}
                for connector in (live.get('connectors', []) if live.get('direct_goal_actions') else channel['snapshot'].get('connectors', [])):
                    key = resource_id(channel['id'], connector['source'], connector['account'])
                    resources[key] = dict(id=key, channel_id=channel['id'], source_id=connector['source'],
                        account_id=connector['account'], name=connector['account'], operations=[
                            dict(id=operation, label=label, available=bool(live.get('direct_goal_actions') and connector.get('online') and operation in connector.get('capabilities', [])),
                                 reason='' if live.get('direct_goal_actions') and connector.get('online') and operation in connector.get('capabilities', []) else '目标执行适配尚未接通或所选账号执行器离线')
                            for operation, label in OPERATIONS.items()])
            # Read account records from the authenticated workbench. Never select
            # the workbench's default account as a substitute for an explicit ID.
            try:
                accounts = self._api('xianyu-workbench', '/api/accounts').get('items', [])
                for account in accounts:
                    account_id = str(account['id'])
                    existing = next((r for r in resources.values() if r['source_id'] == 'xianyu' and r['account_id'] == account_id), None)
                    channel = existing['channel_id'] if existing else 'workbench'
                    key = resource_id(channel, 'xianyu', account_id)
                    resources[key] = dict(id=key, channel_id=channel, source_id='xianyu', account_id=account_id,
                        name=account.get('label') or account.get('name') or account.get('nickname') or '闲鱼账号',
                        operations=[dict(id=op, label=label, available=op in {'product.read', 'conversation.read', 'message.read'} or bool(existing and next((o['available'] for o in existing['operations'] if o['id'] == op), False)),
                            reason='' if op in {'product.read', 'conversation.read', 'message.read'} else
                            ('公开搜索适配尚未接通' if op == 'public.search' else '本目标执行适配尚未接通'))
                            for op, label in OPERATIONS.items()])
                    choices = {'resource_ids': [], 'conversation_ids': []}
                    for path, scope_key in [('/api/products', 'resource_ids'), ('/api/conversations', 'conversation_ids')]:
                        try:
                            rows = self._api('xianyu-workbench', path, account_id).get('items', [])
                            choices[scope_key] = [dict(id=str(row['id']), label=row.get('title') or row.get('name') or
                                ('已收到的会话' if scope_key == 'conversation_ids' else '已登记商品'))
                                for row in rows[:100] if row.get('id')]
                        except Exception:
                            pass
                    resources[key]['scope_options'] = choices
            except Exception:
                pass  # Unavailable account sources are not invented.
            if self.xhs_owner_id:
                try:
                    for account in self._xhs('catalog').get('items', []):
                        account_id = account['id']
                        key = resource_id('workbench', 'xhs', account_id)
                        resources[key] = dict(id=key, channel_id='workbench', source_id='xhs', account_id=account_id,
                            name=account.get('name') or '小红书账号', operations=[dict(id=op, label=label,
                                available=op in {'profile.read', 'content.read'} and account.get('status') == 'active',
                                reason='' if op in {'profile.read', 'content.read'} and account.get('status') == 'active'
                                else '账号连接未确认或目标执行适配尚未接通') for op, label in OPERATIONS.items()])
                except Exception:
                    pass
            for account in overview['accounts']:
                key = resource_id(account['channel_id'], account['source_id'], account['account_id'])
                if key in resources:
                    resources[key]['name'] = account['name']
            self.cached = dict(resources=list(resources.values()), agents=list(agents.values()))
            self.expires = time.time() + 15
            return self.cached

    def execute(self, goal, action):
        if action.get('kind') == 'execute_resource':
            return self.channel_executor.execute(goal, action)
        args = action.get('arguments', {})
        key, operation = args.get('resource_id'), args.get('operation')
        resource = next((r for r in self()['resources'] if r['id'] == key), None)
        grant = next((g for g in goal.get('grants', []) if g.get('resource_id') == key and operation in g.get('operations', [])), None)
        capability = next((op for op in resource.get('operations', []) if op['id'] == operation), None) if resource else None
        if not resource or not grant or not capability or not capability['available']:
            raise ValueError('目标没有授权当前账号的这项可用操作')
        if resource['source_id'] == 'xhs' and operation in ('profile.read', 'content.read'):
            return dict(source='authenticated_platform_adapter', resource_id=key, operation=operation,
                        data=self._xhs(operation, resource['account_id']))
        if resource['source_id'] != 'xianyu':
            raise ValueError('该平台目标读取适配尚未接通')
        # Resolve the selected account again on every execution; a stale catalog
        # cannot authorize an account that has disappeared from the workbench.
        account = resource['account_id']
        current = self._api('xianyu-workbench', '/api/accounts').get('items', [])
        if not any(str(a['id']) == account for a in current):
            raise ValueError('执行账号当前不可用')
        scope = grant.get('scope') or {}
        if operation in ('product.read', 'conversation.read'):
            path = '/api/products' if operation == 'product.read' else '/api/conversations'
            data = self._api('xianyu-workbench', path, account)
            permitted = scope.get('resource_ids' if operation == 'product.read' else 'conversation_ids', [])
            items = data.get('items', [])
            if permitted:
                items = [x for x in items if str(x.get('id')) in permitted]
            return dict(source='authenticated_workbench_cache', resource_id=key, operation=operation,
                        items=items[:100], limited=len(items) > 100)
        if operation == 'message.read':
            conversation = str(args.get('conversation_id', ''))
            if not conversation or len(conversation) > 160 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:-' for c in conversation):
                raise ValueError('请指定授权会话')
            if conversation not in scope.get('conversation_ids', []):
                raise ValueError('该会话不在目标的明确授权范围内')
            rows = self._api('xianyu-workbench', '/api/conversations', account).get('items', [])
            if not any(str(x.get('id')) == conversation for x in rows):
                raise ValueError('会话不属于所选账号')
            data = self._api('xianyu-workbench', '/api/conversations/' + conversation + '/messages', account)
            return dict(source='authenticated_workbench_cache', resource_id=key, operation=operation, data=data)
        raise ValueError('目标操作当前不可用')

    def poll(self, goal, action):
        return self.channel_executor.poll(goal, action)

    def cancel(self, goal, action):
        return self.channel_executor.cancel(goal, action)

    def sync_goal(self, goal):
        return self.channel_executor.sync_goal(goal)
