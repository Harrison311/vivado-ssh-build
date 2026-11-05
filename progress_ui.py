"""Rich live stage display; durations come from actual server run markers."""
from concurrent.futures import ThreadPoolExecutor
import time

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

SPINNERS = {}


def elapsed(seconds):
    if seconds is None:
        return '--:--'
    minutes, seconds = divmod(int(seconds), 60)
    return '%02d:%02d' % (minutes, seconds)


def render_status(part, job, status, received_at, download=None, notice=''):
    table = Table.grid(padding=(0, 2), expand=True)
    table.add_column(width=2)
    table.add_column(ratio=1)
    table.add_column(justify='right')
    rows = list(status.get('progress', {}).get('stages', []))
    if not rows:
        rows = [{'name': 'Waiting for server status', 'state': 'RUNNING', 'seconds': 0}]
    if download:
        rows.append(download)
    for row in rows:
        state = row['state']
        age = row.get('seconds')
        if state == 'RUNNING' and age is not None:
            age += max(0, time.monotonic() - received_at)
        if state == 'RUNNING':
            icon = SPINNERS.setdefault(row['name'], Spinner('dots', style='green'))
            style = 'bold white'
        else:
            symbols = {'DONE': ('✓', 'green'), 'FAILED': ('✗', 'red'),
                       'PENDING': ('○', 'dim'), 'NOT RUN': ('−', 'dim'), 'UNKNOWN': ('?', 'yellow')}
            symbol, style = symbols.get(state, ('?', 'yellow'))
            icon = Text(symbol, style=style)
        label = elapsed(age) if state not in ('PENDING', 'NOT RUN') else state
        table.add_row(icon, Text(row['name'], style=style), Text(label, style=style))
    title = Text('vivado-build · ' + part, style='bold cyan')
    footer = Text('Job: ' + job + '\n' + (notice or status.get('progress', {}).get('current', 'Starting')),
                  style='dim')
    return Panel(Group(title, Text(''), table, Text(''), footer), border_style='blue')


def watch_and_fetch(client, job):
    console = Console()
    interval = max(1, float(client.cfg.get('PollSeconds', 3)))
    status = {'state': 'STARTING'}
    sampled = time.monotonic()
    failures = 0
    notice = ''
    # One outstanding status request. Live's refresh thread animates during SSH I/O.
    with ThreadPoolExecutor(max_workers=1) as pool, Live(console=console, refresh_per_second=10) as live:
        next_poll = 0
        future = None
        while status['state'] in ('STARTING', 'RUNNING'):
            now = time.monotonic()
            if future is None and now >= next_poll:
                future = pool.submit(client.call, 'status', job=job)
            if future is not None and future.done():
                try:
                    status = future.result()
                    failures = 0
                    notice = ''
                    sampled = now
                except Exception as exc:
                    failures += 1
                    notice = 'Status query retry %d/3: %s' % (failures, str(exc))
                    if failures >= 3:
                        raise RuntimeError('Status connection failed; remote job may still run. Use status/fetch later.') from exc
                future = None
                next_poll = now + interval
            live.update(render_status(client.cfg['Part'], job, status, sampled, notice=notice))
            time.sleep(0.1)
        # Failed jobs also fetch diagnostic results automatically.
        download_started = time.monotonic()
        future = pool.submit(client.fetch, job)
        while not future.done():
            download = {'name': 'Download results', 'state': 'RUNNING', 'seconds': time.monotonic() - download_started}
            live.update(render_status(client.cfg['Part'], job, status, time.monotonic(), download))
            time.sleep(0.1)
        try:
            future.result()
        except Exception:
            live.update(render_status(client.cfg['Part'], job, status, time.monotonic(),
                        {'name': 'Download results', 'state': 'FAILED', 'seconds': time.monotonic() - download_started}))
            raise
        live.update(render_status(client.cfg['Part'], job, status, time.monotonic(),
                    {'name': 'Download results', 'state': 'DONE', 'seconds': time.monotonic() - download_started}))
    if status['state'] != 'PASSED':
        console.print(Text(status.get('log_tail', ''), style='red'))
        raise RuntimeError('Build %s; diagnostic results downloaded.' % status['state'])
    console.print('[green]Build passed; results downloaded.[/green]')
