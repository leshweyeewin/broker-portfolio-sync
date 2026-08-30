import sys
sys.path.insert(0, '')
from config.settings import get_spreadsheet_id, get_service_account_info
from sheets.writer import SheetClient
from lemon8.reader import read_closed_positions
from pancherry_export.exporter import build_weekly_journal, _write_journal
import datetime

client = SheetClient(get_service_account_info(), get_spreadsheet_id())
closed = read_closed_positions(client)
repo = r'D:\Learn\Google\pancherry'

for d in ['2026-08-16', '2026-08-23', '2026-08-30']:
    dt = datetime.datetime.strptime(d, '%Y-%m-%d')
    j = build_weekly_journal(closed, today=dt, window_days=7)
    
    # Write journal unconditionally
    _write_journal(repo, j)
    print(f'Wrote {j["slug"]}')
