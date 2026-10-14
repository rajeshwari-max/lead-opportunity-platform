"""Configure the nightly full scrape: 00:00 India time, five sources at a time.

Every page of every source is walked. The run is health-checked at 06:00 and
stopped at 08:00 at the latest (LOP_SCRAPE_HEALTH_CHECK_AT /
LOP_SCRAPE_HARD_STOP_AT), so nothing scrapes during the day.

Run from backend with the API stopped, then restart the API. Does not scrape,
modify opportunity data, or send email.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
import shutil


def configure(root: Path):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    env = root / '.env'
    updates = {
        'LOP_FULL_SOURCE_SCAN': 'true',
        'LOP_MAX_CONCURRENT_SOURCES': '5',
        'LOP_SCHEDULER_CATCHUP_ON_RESTART': 'false',
    }
    lines = env.read_text(encoding='utf-8-sig').splitlines() if env.exists() else []
    if env.exists():
        shutil.copy2(env, env.with_name(f'.env.before-full-scan-{stamp}'))
    lines = [line for line in lines if line.partition('=')[0].strip() not in updates]
    lines.extend(f'{key}={value}' for key, value in updates.items())
    env.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    state_path = root / 'data' / 'schedule.json'
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    if state_path.exists():
        shutil.copy2(state_path, state_path.with_name(f'schedule.before-full-scan-{stamp}.json'))
    state['config'] = dict(mode='daily', cron=None, hour=0,
                           minute=0, timezone='Asia/Kolkata')
    temporary = state_path.with_suffix('.json.next')
    temporary.write_text(json.dumps(state, indent=2), encoding='utf-8')
    temporary.replace(state_path)
    print('Configured: nightly full scrape at 00:00 Asia/Kolkata, five sources '
          'at a time; health check 06:00; stopped at 08:00 at the latest.')
    print('Restart the API to load these settings.')


if __name__ == '__main__':
    configure(Path(__file__).resolve().parents[1])
