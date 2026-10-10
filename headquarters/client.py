"""Server-only Channel control client; URLs and credentials are local config."""
import json
from pathlib import Path
from urllib.parse import urlsplit
import requests


class ChannelClient:
    def __init__(self, config):
        self.url = config['base_url'].rstrip('/')
        parsed = urlsplit(self.url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('Channel URL must be an origin')
        if parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', '::1', 'localhost'):
            raise ValueError('Remote Channel connections require HTTPS')
        self.key_file = Path(config['token_file'])

    def request(self, method, path, data=None):
        response = requests.request(method, self.url + path, json=data,
            headers={'Authorization': 'Bearer ' + self.key_file.read_text().strip()}, timeout=(3, 8), allow_redirects=False)
        if response.status_code not in (200, 201, 202):
            raise RuntimeError('Channel request not confirmed: ' + str(response.status_code))
        return response.json()

    def catalog(self):
        return self.request('GET', '/control/v1/catalog')

    def state(self, after):
        return self.request('GET', '/control/v1/state?after=' + str(after))

    def deploy(self, goal_id, data):
        return self.request('PUT', '/control/v1/deployments/' + goal_id, data)

    def renew(self, rows):
        return self.request('POST', '/control/v1/renewals', {'items': rows})

    def decision(self, action_id, approve):
        return self.request('POST', '/control/v1/actions/' + action_id + '/decision', {'approve': approve})


def load_clients(path):
    rows = json.loads(Path(path).read_text())
    if not isinstance(rows, dict) or not rows or len(rows) > 20:
        raise ValueError('Configure 1–20 named Channel connections')
    return {key: ChannelClient(value) for key, value in rows.items()}
