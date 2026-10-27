"""Server-only planner configuration; authentication stays with Multica."""
import json
from pathlib import Path
from .autonomy import AutonomyStore, ModelPlanner, MulticaAdapter, MulticaPlanner
from .business_executor import BusinessClient, WorkbenchGoalExecutor


def create_autonomy(directory, capabilities):
    directory = Path(directory)
    file = directory / 'autonomy.json'
    config = json.loads(file.read_text()) if file.is_file() else {}
    capabilities.xhs_owner_id = config.get('xhs_owner_id')
    adapter = MulticaAdapter(config['multica_config_file']) if config.get('multica_config_file') else None
    planner = None
    if config.get('planner_agent_id') and adapter:
        planner = MulticaPlanner(adapter, config['planner_agent_id'], timeout=config.get('planner_timeout', 240))
    elif config.get('model'):
        planner = ModelPlanner(config['model'])
    if config.get('workbenches'):
        clients={name:BusinessClient(value) for name,value in config['workbenches'].items()}
        capabilities=WorkbenchGoalExecutor(directory,clients,legacy=capabilities.channel_executor,channels=capabilities.hq.clients,
            agents_provider=(lambda:adapter.request('GET','/api/agents')) if adapter else None)
    return AutonomyStore(directory, planner=planner, multica=adapter,
        capability_provider=capabilities, executor=capabilities.execute,
        poller=capabilities.poll, canceller=capabilities.cancel)
