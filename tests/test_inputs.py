import csv
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'

class InputSetTests(unittest.TestCase):
    def test_csv_inputs_have_headers_and_records(self):
        files = sorted(DATA.glob('*.csv'))
        self.assertGreaterEqual(len(files), 3)
        for path in files:
            with path.open(encoding='utf-8', newline='') as stream:
                reader = csv.DictReader(stream)
                self.assertTrue(reader.fieldnames, path.name)
                rows = list(reader)
                self.assertTrue(rows, path.name)
                self.assertTrue(all(set(row) == set(reader.fieldnames) for row in rows), path.name)

    def test_row_identifiers_are_unique_within_each_file(self):
        unique_keys = {'event_id', 'ticket_id', 'request_id', 'attempt_id', 'touch_id', 'order_id', 'creative_id', 'reading_id', 'followup_id', 'store_id', 'gateway', 'session_id', 'probe_id', 'case_id', 'site_id', 'observation_id', 'commitment_id', 'meeting_id', 'summary_id', 'action_id', 'review_id', 'permission_id', 'application_id', 'receipt_id', 'task_id'}
        for path in DATA.glob('*.csv'):
            with path.open(encoding='utf-8', newline='') as stream:
                reader = csv.DictReader(stream)
                first = reader.fieldnames[0]
                identifiers = [row[first] for row in reader]
                if first in unique_keys:
                    self.assertEqual(len(identifiers), len(set(identifiers)), path.name)

if __name__ == '__main__':
    unittest.main()
