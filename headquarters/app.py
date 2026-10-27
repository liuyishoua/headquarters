import os
import secrets
import time
from flask import Flask, jsonify, redirect, request, session, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from .core import Headquarters, Problem
from .autonomy import AutonomyStore, AutonomyError
from .capabilities import CapabilityDirectory


def private_file(path, value):
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(value)
    path.chmod(0o600)


def create_app(directory, clients, origin='', auth=True, background=False, autonomy=None, autonomy_factory=None):
    hq = Headquarters(directory, clients)
    directory_adapter = CapabilityDirectory(hq)
    autonomy = autonomy or (autonomy_factory(directory, directory_adapter) if autonomy_factory else
        AutonomyStore(directory, capability_provider=directory_adapter, executor=directory_adapter.execute,
            poller=directory_adapter.poll, canceller=directory_adapter.cancel))
    private_file(hq.directory / 'session-key', secrets.token_hex(32))
    private_file(hq.directory / 'admin-password', secrets.token_urlsafe(24))
    password_hash = generate_password_hash((hq.directory / 'admin-password').read_text().strip())
    app = Flask(__name__, static_folder='static')
    from .page_assets import install_pages
    install_pages(app, {'index.html','login.html','automation.html'})
    app.config.update(SECRET_KEY=(hq.directory / 'session-key').read_text(), SESSION_COOKIE_NAME='headquarters_console',
        SESSION_COOKIE_SECURE=origin.startswith('https:'), SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
        MAX_CONTENT_LENGTH=128 * 1024, PERMANENT_SESSION_LIFETIME=43200)
    app.extensions['headquarters'] = hq
    app.extensions['autonomy'] = autonomy
    attempts = {}

    @app.before_request
    def guard():
        if request.path == '/healthz':
            return
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if request.headers.get('Origin', '').rstrip('/') != (origin or request.host_url.rstrip('/')) or request.headers.get('X-App-Request') != '1':
                raise Problem('请求来源无效', 403)
            if not request.is_json or not isinstance(request.get_json(silent=True), dict):
                raise Problem('请求必须是 JSON 对象')
        if request.path in ('/login', '/api/login') or request.path.startswith('/static/'):
            return
        if auth and not session.get('authenticated'):
            if request.path.startswith('/api/'):
                raise Problem('请先登录指挥部', 401)
            return redirect('/login')

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"})
        return response

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, Problem):
            return jsonify(error=str(exc)), exc.status
        if isinstance(exc, AutonomyError):
            return jsonify(error=str(exc)), 400
        if isinstance(exc, LookupError):
            return jsonify(error='记录不存在'), 404
        if isinstance(exc, HTTPException):
            return jsonify(error='请求无效或内容过大'), exc.code
        app.logger.error('Headquarters API error: %s', type(exc).__name__)
        return jsonify(error='操作未完成，请检查服务连接'), 502

    @app.get('/healthz')
    def health():
        with hq.db() as db:
            db.execute('SELECT 1')
        return {'ok': True}

    @app.get('/login')
    def login_page():
        return app.send_static_file('login.html')

    @app.post('/api/login')
    def login():
        now = time.time(); ip = request.remote_addr
        attempts[ip] = [x for x in attempts.get(ip, []) if now - x < 60]
        if len(attempts[ip]) >= 8:
            raise Problem('尝试过于频繁，请稍后再试', 429)
        if auth and not check_password_hash(password_hash, str(request.json.get('password', ''))):
            attempts[ip].append(now)
            raise Problem('访问密码不正确', 401)
        session.clear(); session['authenticated'] = True; session.permanent = True
        return {'ok': True}

    @app.post('/api/logout')
    def logout():
        session.clear()
        return {'ok': True}

    @app.get('/')
    def index():
        return app.send_static_file('index.html')

    @app.get('/api/overview')
    def overview():
        return hq.overview()

    @app.get('/api/autonomy')
    def autonomy_overview():
        return {'goals': autonomy.list()}

    @app.get('/api/autonomy/capabilities')
    def autonomy_capabilities():
        return autonomy.capabilities()

    @app.post('/api/autonomy')
    def autonomy_create():
        return autonomy.create(request.json), 201

    @app.get('/api/autonomy/<goal_id>')
    def autonomy_get(goal_id):
        return autonomy.get(goal_id)

    @app.post('/api/autonomy/<goal_id>/control')
    def autonomy_control(goal_id):
        return autonomy.control(goal_id, request.json.get('operation'))

    @app.put('/api/autonomy/<goal_id>/grants')
    def autonomy_grants(goal_id):
        return autonomy.update_grants(goal_id, request.json.get('grants', []))

    @app.post('/api/autonomy/<goal_id>/feedback')
    def autonomy_feedback(goal_id):
        note = request.json.get('note')
        if not isinstance(note, str) or not note.strip() or len(note) > 5000:
            raise Problem('请填写最多 5000 字的反馈')
        autonomy.wake(goal_id, 'user_feedback', {'note': note.strip()})
        return autonomy.get(goal_id)

    @app.post('/api/autonomy/<goal_id>/confirm')
    def autonomy_confirm(goal_id):
        return autonomy.confirm_delivery(goal_id, request.json.get('evidence_ids', []), request.json.get('note'))

    @app.get('/api/autonomy/<goal_id>/artifacts/<name>')
    def autonomy_artifact(goal_id, name):
        goal = autonomy.get(goal_id)
        artifact = next((x for x in goal['evidence'] if x['kind'] == 'artifact' and x['data'].get('name') == name), None)
        if not artifact:
            raise LookupError()
        path = autonomy.artifacts / goal_id / name
        if path.parent.resolve() != (autonomy.artifacts / goal_id).resolve() or not path.is_file():
            raise LookupError()
        return send_file(path, as_attachment=True, download_name=name)

    @app.post('/api/missions')
    def create_mission():
        return hq.save_mission(request.json), 201

    @app.put('/api/missions/<mission_id>')
    def edit_mission(mission_id):
        return hq.save_mission(request.json, mission_id)

    @app.post('/api/missions/<mission_id>/transition')
    def transition(mission_id):
        return hq.transition(mission_id, request.json.get('operation'))

    @app.post('/api/missions/<mission_id>/assignments')
    def assign(mission_id):
        return hq.assign(mission_id, request.json), 201

    @app.delete('/api/assignments/<assignment_id>')
    def remove(assignment_id):
        return hq.remove_assignment(assignment_id)

    @app.post('/api/pause-all')
    def pause_all():
        result = hq.pause_all()
        paused = 0
        for goal in autonomy.list():
            if goal['status'] not in ('paused', 'stopped', 'completed'):
                autonomy.control(goal['id'], 'pause')
                paused += 1
        result['paused'] += paused
        return result

    @app.post('/api/accounts')
    def account():
        return hq.save_account(request.json), 201

    @app.post('/api/missions/<mission_id>/outcomes')
    def outcome(mission_id):
        return hq.outcome(mission_id, request.json), 201

    @app.post('/api/actions/<channel_id>/<action_id>/decision')
    def decision(channel_id, action_id):
        return hq.decide_action(channel_id, action_id, request.json.get('approve'))

    if background:
        hq.start_worker()
        autonomy.start_worker(hq.stop_event)
    return app
