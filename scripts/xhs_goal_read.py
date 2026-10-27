"""Fixed, owned-account XHS read bridge using the existing supported adapter.

Invoked with XHS's own virtualenv. No cookie/token is printed or persisted.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2] / 'xhs'
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['catalog', 'profile.read', 'content.read'])
    parser.add_argument('--owner', type=int, required=True)
    parser.add_argument('--account', type=int)
    args = parser.parse_args()
    from sqlalchemy import select
    from backend.app.core.database import SessionLocal
    from backend.app.models.platform_account import PlatformAccount
    from backend.app.models.user import User
    with SessionLocal() as db:
        owner = db.get(User, args.owner)
        if owner is None:
            raise ValueError('Configured workbench owner does not exist')
        rows = db.scalars(select(PlatformAccount).where(PlatformAccount.user_id == owner.id,
            PlatformAccount.platform == 'xhs', PlatformAccount.sub_type == 'pc')).all()
        if args.operation == 'catalog':
            output = {'items': [{'id': str(row.id), 'external_user_id': row.external_user_id,
                'name': row.nickname, 'status': row.status} for row in rows]}
        else:
            row = next((row for row in rows if row.id == args.account), None)
            if row is None:
                raise ValueError('Selected account is not an owned PC account')
            from backend.app.api.platforms.xhs.pc import _get_owned_pc_account_cookies
            from backend.app.adapters.xhs.pc_api_adapter import XhsPcApiAdapter
            adapter = XhsPcApiAdapter(_get_owned_pc_account_cookies(db, owner, row.id))
            if args.operation == 'profile.read':
                response = adapter.get_self_info()
                if response.get('guest') or response.get('user_id') != row.external_user_id:
                    raise ValueError('Platform account identity did not match selected resource')
                output = {k: response[k] for k in ('user_id', 'nickname', 'desc', 'red_id', 'avatar') if k in response}
            else:
                response = adapter.get_recent_user_notes(row.external_user_id)
                # Strip platform access/signing material and keep only published
                # public card fields from the actual adapter response.
                notes = response.get('notes', [])
                output = {'items': [{k: note[k] for k in ('note_id', 'display_title', 'type', 'title', 'desc') if k in note}
                    for note in notes if isinstance(note, dict)], 'has_more': response.get('has_more'),
                    'coverage': 'recent_first_page', 'source': 'authenticated_platform_adapter'}
        print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(json.dumps({'error': 'Selected XHS owned-account read was not confirmed'}))
        raise SystemExit(1)
