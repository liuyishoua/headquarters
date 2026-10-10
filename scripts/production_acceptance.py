"""Production read-only browser acceptance plus temporary portal account."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from waitress import create_server
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT.parent/'bifrost'))
from portal import create_app, password_hash


def main():
    base='https://d.58nft.net:9457';errors=[];server=None
    out=ROOT/'docs/previews'
    with tempfile.TemporaryDirectory(prefix='headquarters-portal-') as directory:
        with patch.dict(os.environ,{'BIFROST_PUBLIC_ORIGIN':'http://127.0.0.1:18875'}):
            portal=create_app(Path(directory),links_path=ROOT.parent/'bifrost/links.json')
        with sqlite3.connect(Path(directory)/'portal.sqlite3') as db:
            db.execute("INSERT INTO users(username,password_hash,role,status,created_at) VALUES(?,?,'user','active',0)",('qa-user',password_hash('synthetic-portal-password')))
        server=create_server(portal,host='127.0.0.1',port=18875);threading.Thread(target=server.run,daemon=True).start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True);context=browser.new_context(viewport={'width':1440,'height':1000});page=context.new_page()
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto('http://127.0.0.1:18875/');page.locator('[name=username]').fill('qa-user');page.locator('[name=password]').fill('synthetic-portal-password');page.locator('button.full-button').click()
                link=page.locator('a.app-link[href="'+base+'/"]');link.wait_for()
                assert link.get_attribute('target')=='_blank'
                with page.expect_popup() as popup:link.click()
                page=popup.value;page.on('pageerror',lambda e:errors.append(str(e)))
                page.locator('#login-form').wait_for();assert page.url==base+'/login'
                page.locator('[name=password]').fill('8'*8);page.locator('#login-form button').click();page.locator('#metrics .metric').first.wait_for()
                assert page.request.get(base+'/api/overview').status==200
                state=page.request.get(base+'/api/overview').json()
                channel=next(c for c in state['channels'] if c['id']=='social')
                assert not channel['error'] and len(channel['snapshot']['sources'])>=8
                assert all(m['state']!='active' for m in state['missions'])
                assert state['accounts']==[]
                for tab in ('missions','accounts','channels','actions','audit'):
                    page.locator('[data-tab='+tab+']').click();assert page.locator('[data-tab='+tab+'].selected').count()==1
                page.locator('[data-tab=channels]').click();page.screenshot(path=str(out/'headquarters-production.png'),full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                for tab in ('missions','accounts','channels','actions','audit'):
                    page.locator('[data-tab='+tab+']').click();assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),tab
                page.locator('[data-tab=missions]').click();page.screenshot(path=str(out/'headquarters-production-mobile.png'),full_page=True)
                page.set_viewport_size({'width':1440,'height':1000});page.locator('#logout').click();page.locator('#login-form').wait_for();assert page.request.get(base+'/api/overview').status==401
                # Channel compatibility and its link back to Headquarters.
                page.goto('https://d.58nft.net:9456/login');page.locator('[name=password]').fill('8'*8);page.locator('#login-form button').click();page.wait_for_url('https://d.58nft.net:9456/')
                page.goto('https://d.58nft.net:9456/automation');page.get_by_role('heading',name='账号目标与执行').wait_for();assert page.locator('a[href="'+base+'/"]').count()>=1
                assert not errors,errors;browser.close()
            report=dict(passed=True,https_login_logout=True,portal_click=True,portal_account='temporary synthetic only',production_accounts_unchanged=True,production_missions_active=0,channel_connected=True,platform_sources=len(channel['snapshot']['sources']),tabs=5,mobile_overflow=False,browser_errors=errors)
            (ROOT/'docs/production-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False,indent=2))
        finally:
            if server:server.close()

if __name__=='__main__':main()
