"""Isolated autonomous-goal UI acceptance with synthetic API responses only."""
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    goals = []
    requests_seen = []
    errors = []
    overview = dict(missions=[], accounts=[], audit=[], channels=[dict(id='synthetic', snapshot=dict(agents=[dict(id='synthetic-agent', name='测试指挥员')]))])
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(ROOT / 'headquarters')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    previews = ROOT / 'docs/previews'
    previews.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport=dict(width=1440, height=1100))
            page.on('pageerror', lambda error: errors.append(str(error)))

            def respond(route):
                request = route.request
                path = request.url.split('/api/', 1)[1]
                if path == 'autonomy/capabilities':
                    data = dict(resources=[dict(id='qa-xhs', name='合成小红书账号', platform='xhs', operations=[dict(id='discover', label='搜索公开需求', available=True), dict(id='publish', label='发布作品', available=False, reason='未接入发布接口')]), dict(id='qa-xianyu', name='合成闲鱼账号', platform='xianyu', operations=[dict(id='read', label='读取商品', available=True), dict(id='message.read', label='读取授权消息', available=True)], scope_options=dict(conversation_ids=[dict(id='owned-chat', label='已授权客户对话')]))], agents=[dict(id='synthetic-agent', name='测试指挥员')])
                elif path.endswith('/feedback'):
                    goals[0]['observations'].append(dict(kind='user_feedback', data=dict(note=request.post_data_json['note'])))
                    data = goals[0]
                elif path.endswith('/grants'):
                    goals[0]['grants'] = request.post_data_json['grants']
                    data = goals[0]
                elif path == 'overview':
                    data = overview
                elif path == 'autonomy' and request.method == 'GET':
                    data = dict(goals=goals)
                elif path == 'autonomy':
                    payload = request.post_data_json
                    requests_seen.append(payload)
                    goals.append(dict(id='synthetic-goal', **payload, status='running', summary='正在研究需求，尚无成交证据。', next_action='比较两种公开销售渠道', next_check=1900000000, actions=[], evidence=[], observations=[]))
                    data = goals[-1]
                elif path.endswith('/control'):
                    operation = request.post_data_json['operation']
                    goals[0]['status'] = dict(pause='paused', resume='running', stop='stopped')[operation]
                    data = goals[0]
                else:
                    raise AssertionError(path)
                route.fulfill(status=200, content_type='application/json', body=json.dumps(data, ensure_ascii=False))

            page.route('**/api/**', respond)
            page.goto(f'http://127.0.0.1:{server.server_port}/static/index.html')
            page.get_by_role('heading', name='给一个目标，持续推进。').wait_for()
            page.locator('#create-mission').click()
            form = page.locator('#goal-form')
            form.locator('[name=title]').fill('游泳卡转让 · 合成验收')
            form.locator('[name=objective]').fill('寻找有明确意愿的买家，所有账号和反馈均为虚构测试资料。')
            form.locator('[name=completion_criteria]').fill('买家确认条件并有可核验沟通证据。')
            form.locator('[name=constraints]').fill('使用合成账号，不发送真实消息。')
            form.locator('[name=agent_id]').select_option('synthetic-agent')
            assert form.locator('[name=grant_operation]:checked').count() == 0
            assert form.locator('[name=grant_operation][value=publish]').is_disabled()
            form.locator('[name=grant_operation][value=discover]').check()
            form.locator('[name=grant_operation][value=read]').check()
            form.locator('[name=grant_operation][value="message.read"]').check()
            form.locator('.grant-scope summary').click()
            form.locator('[name=grant_scope_conversation_ids][value=owned-chat]').check()
            form.get_by_role('button', name='登记并开始推进').click()
            page.locator('#modal').wait_for(state='hidden')
            page.get_by_text('正在研究需求，尚无成交证据。').wait_for()
            assert requests_seen[0]['limits']['max_actions'] == 20
            assert requests_seen[0]['grants'] == [dict(resource_id='qa-xhs', operations=['discover'], scope=dict(resource_ids=[], conversation_ids=[], contact_ids=[], include_goal_created=False)), dict(resource_id='qa-xianyu', operations=['read','message.read'], scope=dict(resource_ids=[], conversation_ids=['owned-chat'], contact_ids=[], include_goal_created=False))]
            page.get_by_role('button', name='调整授权').click()
            grant_form = page.locator('#goal-grants-form')
            grant_form.wait_for()
            assert grant_form.locator('[name=grant_operation]:checked').count() == 3
            assert grant_form.locator('[name=grant_scope_conversation_ids][value=owned-chat]').is_checked()
            grant_form.locator('[value=discover]').uncheck()
            grant_form.get_by_role('button', name='保存授权').click()
            page.locator('#modal').wait_for(state='hidden')
            page.get_by_role('button', name='调整授权').click()
            grant_form = page.locator('#goal-grants-form')
            grant_form.wait_for()
            assert not grant_form.locator('[value=discover]').is_checked()
            assert grant_form.locator('[value=read]').is_checked()
            assert grant_form.locator('[name=grant_scope_conversation_ids][value=owned-chat]').is_checked()
            page.locator('#close-modal').click()
            assert requests_seen[0]['constraints'] == '使用合成账号，不发送真实消息。'
            page.get_by_role('button', name='补充反馈').click()
            feedback = page.locator('#goal-feedback-form')
            feedback.locator('[name=note]').fill('合成反馈：需要调整地区范围。')
            feedback.get_by_role('button', name='提交反馈').click()
            page.locator('#modal').wait_for(state='hidden')
            assert goals[0]['observations'][-1]['data']['note'] == '合成反馈：需要调整地区范围。'
            page.get_by_role('button', name='暂停推进').click()
            page.get_by_role('button', name='继续推进').wait_for()
            page.get_by_role('button', name='继续推进').click()
            page.get_by_role('button', name='暂停推进').wait_for()
            goals[0].update(blocker='需要确认卡片能否转让。', actions=[dict(summary='比较公开需求', status='succeeded', result='发现需求与使用范围不匹配，调整研究方向。', evidence=['合成研究记录'])], observations=['下一轮缩小地区范围。'], evidence=[dict(kind='artifact', data=dict(name='report.md'))])
            page.locator('#refresh').click()
            page.get_by_text('需要确认卡片能否转让。').wait_for()
            page.get_by_text('合成研究记录').wait_for()
            assert page.get_by_role('link', name='下载 report.md').first.get_attribute('href').endswith('/artifacts/report.md')
            page.screenshot(path=str(previews / 'headquarters-autonomy-desktop.png'), full_page=True)
            page.set_viewport_size(dict(width=390, height=844))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(previews / 'headquarters-autonomy-mobile.png'), full_page=True)
            page.get_by_role('button', name='停止目标').click()
            page.locator('.detail').get_by_text('已停止', exact=True).wait_for()
            assert page.get_by_role('button', name='暂停推进').count() == 0
            page.locator('[data-tab=missions]').click()
            page.get_by_role('heading', name='先确定想完成什么。').wait_for()
            page.locator('[data-tab=autonomy]').click()
            page.locator('#create-mission').click()
            form = page.locator('#goal-form')
            form.wait_for()
            form.locator('[name=title]').fill('合成文档目标')
            form.locator('[name=objective]').fill('根据已有资料整理研究报告。')
            form.locator('[name=delivery_type]').select_option('local_artifact')
            form.locator('[name=completion_criteria]').fill('报告包含关键发现。')
            form.locator('[name=artifact_name]').fill('report.md')
            form.locator('[name=artifact_contains]').fill('发现一\n发现二')
            form.locator('[name=source_material]').fill('合成研究资料')
            form.locator('[name=agent_id]').select_option('synthetic-agent')
            form.get_by_role('button', name='登记并开始推进').click()
            page.locator('#modal').wait_for(state='hidden')
            assert requests_seen[-1]['artifact_checks'] == {'报告包含关键发现。': {'name':'report.md','contains':['发现一','发现二']}}
            assert requests_seen[-1]['source_material'] == '合成研究资料'
            assert not errors, errors
            browser.close()
        report = dict(passed=True, synthetic_api_only=True, real_platform_calls=False, browser_errors=errors, mobile_overflow=False, checks=['goal submission and constraints', 'document delivery checks, source materials, feedback and artifact links', 'multi-platform operation grants and selective revocation', 'pause and resume', 'action feedback and blockers', 'desktop and mobile screenshots', 'stop', 'manual mission compatibility'])
        (ROOT / 'docs/autonomy-browser-acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
