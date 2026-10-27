import argparse
from pathlib import Path
import signal
import sys
from waitress import serve
from .app import create_app
from .client import load_clients
from .runtime import create_autonomy


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description='Headquarters goal control plane')
    parser.add_argument('--port', type=int, default=8781)
    parser.add_argument('--data-dir', type=Path, default=root / '.local')
    parser.add_argument('--public-origin', default='')
    args = parser.parse_args()
    app = create_app(args.data_dir, load_clients(args.data_dir / 'channels.json'), origin=args.public_origin, background=True,
        autonomy_factory=create_autonomy)
    from .workspace_identity import install_identity
    install_identity(app, args.data_dir, 'headquarters', None, machine_prefixes=('/healthz',))
    def stop(*_):
        app.extensions['headquarters'].stop_event.set()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    serve(app, host='127.0.0.1', port=args.port, threads=12)


if __name__ == '__main__':
    main()
