"""Preview/apply common main-model settings to every local Hermes profile.

Targets the current openai-codex setup and matching Paperclip Hermes agents.
New sessions/runs pick up settings; existing sessions can retain overrides.
No agents are started. Hindsight's processing model is a separate setting.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import urllib.request
import urllib.error
from datetime import datetime
from ruamel.yaml import YAML

from hermes_constants import get_default_hermes_root, parse_reasoning_effort, resolve_reasoning_config
from hermes_cli.config import atomic_config_replace
from hermes_cli.models import resolve_fast_mode_overrides

ROOT = get_default_hermes_root()

def show_help(parser):
    print('HERMES TEAM MODEL SETTINGS\n')
    print('PowerShell usage (from this script folder):')
    print('  .\\Set-HermesTeamModel.ps1')
    print('  .\\Set-HermesTeamModel.ps1 -Model gpt-6.1-sol -Thinking medium -Speed normal')
    print('  .\\Set-HermesTeamModel.ps1 -Model gpt-6.1-sol -Thinking medium -Speed fast -Apply')
    print('\nArguments:')
    print('  -Model     Explicit model ID from the Hermes catalog below; do not use auto.')
    print('  -Thinking  none, minimal, low, medium, high, xhigh, max, ultra')
    print('  -Speed     normal, fast, ultrafast (availability shown per model below)')
    print('  -Apply     Save changes and create backups; omit for a read-only preview.')
    print('  -ApiBase   Paperclip URL; default http://127.0.0.1:3100')
    print('  -HermesOnly  Update Hermes only; do not contact Paperclip.')
    print('  -Help/-h   Display this help and the model catalog.')
    print('\nScope: all local Hermes profiles and matching Paperclip Hermes agents.')
    print('Provider: openai-codex (your existing login). Use fresh sessions/runs afterward.')
    print('Sets each profile default; it does not limit the models in the Hermes picker.')
    print('You can change individual agents afterward; applying this script again resets all defaults.')
    print('Does not start agents or change Hindsight processing settings.')
    print('If Paperclip is offline, Hermes still updates; Paperclip synchronization is skipped.')
    print('Run the same command with -Apply again when Paperclip is online to synchronize it.')
    print('\nHermes model catalog: live discovery when available, otherwise cached/curated.')
    print('Account access still applies. Thinking columns show native Hermes/Codex levels;')
    print('other accepted thinking values may be adjusted by Hermes to a supported level.\n')
    from agent.reasoning_effort import codex_supported_efforts
    from hermes_cli.models import curated_models_for_provider
    try:
        models = [model for model, _ in curated_models_for_provider('openai-codex')]
    except Exception:
        from hermes_cli.codex_models import get_codex_model_ids
        models = get_codex_model_ids()
        print('(Live discovery unavailable; displaying local catalog.)')
    print(f"{'MODEL':<32} {'SPEED':<24} THINKING")
    for model in dict.fromkeys(models):
        speeds = ['normal']
        for label, tier in [('fast','priority'),('ultrafast','ultrafast')]:
            if resolve_fast_mode_overrides(model, provider='openai-codex', tier=tier):
                speeds.append(label)
        print(f"{model:<32} {','.join(speeds):<24} {','.join(codex_supported_efforts(model))}")
    if not models:
        print('No models could be discovered. Check Hermes login/model configuration.')
    print('\nPython equivalents:\n')
    parser.print_help()

def api(base, method, route, body=None):
    request = urllib.request.Request(base.rstrip('/') + '/api' + route,
        method=method, data=None if body is None else json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)

def paperclip_targets(base, names):
    targets = []
    for company in api(base, 'GET', '/companies'):
        for agent in api(base, 'GET', f"/companies/{company['id']}/agents"):
            if agent['adapterType'] != 'hermes_local':
                continue
            cfg = agent['adapterConfig']; extra = cfg.get('extraArgs') or []
            profile = extra[extra.index('--profile')+1] if '--profile' in extra else 'default'
            if profile not in names:
                raise ValueError(f"Paperclip agent {agent['name']} references an unknown profile: {profile}")
            if any(flag in extra for flag in ('-m','--model','--provider','--reasoning','--fast')):
                raise ValueError(f"Paperclip agent {agent['name']} has explicit CLI overrides; review extraArgs first.")
            targets.append({'id':agent['id'],'name':agent['name'], 'profile':profile,
                'model':cfg.get('model'), 'provider':cfg.get('provider')})
    return targets

def change_config(config, model, thinking, tier):
    current = config.get('model')
    if isinstance(current, str):
        config['model'] = {'default': current}
    model_cfg = config.setdefault('model', {})
    if model_cfg.get('provider', 'openai-codex') not in ('auto', 'openai-codex', ''):
        raise ValueError('A profile uses a different provider; review it before switching providers.')
    model_cfg['default'] = model
    model_cfg['provider'] = 'openai-codex'
    # Prevent an old per-request speed override from defeating the shared tier.
    extra = model_cfg.get('extra_body')
    if isinstance(extra, dict):
        for key in ('service_tier', 'speed'):
            extra.pop(key, None)
    agent = config.setdefault('agent', {})
    agent['reasoning_effort'] = thinking
    # Exact-model entries take priority over aliases and existing global values.
    agent.setdefault('reasoning_overrides', {})[model] = thinking
    agent['service_tier'] = tier
    assert resolve_reasoning_config(config, model) == parse_reasoning_effort(thinking)
    return config

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True, help='Explicit model ID; auto is not accepted.')
    parser.add_argument('--thinking', required=True, choices=['none','minimal','low','medium','high','xhigh','max','ultra'])
    parser.add_argument('--speed', required=True, choices=['normal','fast','ultrafast'])
    parser.add_argument('--api-base', default='http://127.0.0.1:3100')
    parser.add_argument('--apply', action='store_true', help='Write changes. Omit to preview only.')
    parser.add_argument('--hermes-only', action='store_true', help='Skip Paperclip entirely. Offline Paperclip is otherwise skipped automatically.')
    if len(sys.argv) == 1 or any(arg in ('-h', '--help') for arg in sys.argv[1:]):
        show_help(parser)
        return
    args = parser.parse_args()
    if not args.model.strip() or args.model.strip().lower() == 'auto':
        parser.error('Use an explicit model ID; literal auto caused the previous adapter failures.')
    tier = {'normal':'', 'fast':'priority', 'ultrafast':'ultrafast'}[args.speed]
    if tier and not resolve_fast_mode_overrides(args.model, provider='openai-codex', tier=tier):
        parser.error('Installed Hermes does not support this model/provider/speed combination.')
    files = ([ROOT / 'config.yaml'] if (ROOT / 'config.yaml').is_file() else [])
    files += sorted((ROOT / 'profiles').glob('*/config.yaml'))
    if not files:
        parser.error('No Hermes profiles found.')
    plans = []
    for path in files:
        yaml = YAML(); yaml.preserve_quotes = True
        original = path.read_bytes()
        config = yaml.load(original.decode('utf-8-sig')) or {}
        current = config.get('model') or {}
        before = current.get('default') if isinstance(current, dict) else current
        change_config(config, args.model, args.thinking, tier)
        plans.append({'name':'default' if path.parent == ROOT else path.parent.name,
            'path':path, 'before':before, 'old':original, 'config':config})
    names = {p['name'] for p in plans}
    targets = []
    paperclip_status = 'skipped (--hermes-only)' if args.hermes_only else 'online'
    if not args.hermes_only:
        try:
            targets = paperclip_targets(args.api_base, names)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            # HTTP authentication/validation errors are not an offline server.
            if isinstance(exc, urllib.error.HTTPError):
                raise
            paperclip_status = 'offline; synchronization skipped'
    print(json.dumps({'mode':'APPLY' if args.apply else 'PREVIEW', 'model':args.model,
        'provider':'openai-codex', 'thinking':args.thinking,'speed':args.speed,
        'hermesProfiles':len(plans),'paperclipAgents':len(targets),'paperclipStatus':paperclip_status,
        'profiles':[{'name':p['name'],'previousModel':p['before']} for p in plans]}, indent=2))
    if not args.apply:
        print('Preview only. Add -Apply in PowerShell (or --apply in Python) to save.')
        return
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    written = []
    synced = []
    try:
        for plan in plans:
            path = plan['path']
            if path.read_bytes() != plan['old']:
                raise RuntimeError(f"Profile changed during preflight: {plan['name']}; retry the preview.")
            backup = path.with_name(path.name + '.before-team-model-' + stamp)
            shutil.copy2(path, backup)
            atomic_config_replace(path, plan['config'])
            written.append(plan['name'])
        try:
            for target in targets:
                fresh = api(args.api_base,'GET','/agents/'+target['id'])['adapterConfig']
                if (fresh.get('model'),fresh.get('provider')) != (target['model'],target['provider']):
                    raise RuntimeError(f"Paperclip settings changed during preflight: {target['name']}.")
                saved = api(args.api_base,'PATCH','/agents/'+target['id'],
                    {'adapterConfig':{'model':args.model,'provider':'openai-codex'}})
                assert saved['adapterConfig']['model'] == args.model
                synced.append(target['name'])
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                raise
            paperclip_status = 'connection lost; remaining synchronization skipped'
        print(f'Saved {len(plans)} Hermes profiles; synchronized {len(synced)} Paperclip agents. Backups: config.yaml.before-team-model-{stamp} alongside each profile config.')
        if paperclip_status != 'online':
            print(f'Paperclip: {paperclip_status}. Hermes changes are saved.')
            print('Paperclip may still override the model with its old settings. Run the same command with -Apply, without -HermesOnly, when Paperclip is online to synchronize it. No automatic retry is scheduled.')
        print('Use fresh Hermes sessions/new Paperclip runs; running processes were not restarted. Hindsight processing settings were not changed.')
    except Exception:
        print(f'Apply stopped; profiles already written: {written}. Backups use suffix .before-team-model-{stamp}. Re-run preview after resolving the error.',file=sys.stderr)
        raise

if __name__ == '__main__':
    main()
