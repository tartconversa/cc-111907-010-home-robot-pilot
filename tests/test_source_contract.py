import hashlib
import json
from datetime import datetime
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]

class SourceContractTests(unittest.TestCase):
    def test_manifest_matches_actual_source_bytes(self):
        manifest=json.loads((ROOT/'inputs/manifest.json').read_text(encoding='utf-8'))
        for filename,digest in manifest['files'].items():
            with self.subTest(filename=filename):
                self.assertEqual(hashlib.sha256((ROOT/'inputs'/filename).read_bytes()).hexdigest(),digest)

    def test_rows_respect_published_fields_and_time_format(self):
        schema=json.loads((ROOT/'inputs/schema.json').read_text(encoding='utf-8'))
        for name,definition in schema.items():
            rows=json.loads((ROOT/'inputs'/f'{name}.json').read_text(encoding='utf-8'))
            self.assertTrue(rows,name)
            for row in rows:
                self.assertEqual(set(row),set(definition['columns']),name)
                for value in row.values():
                    if isinstance(value,str) and len(value)>18 and value[4:5]=='-' and 'T' in value:
                        self.assertIsNotNone(datetime.fromisoformat(value.replace('Z','+00:00')).tzinfo)

    def test_input_files_contain_only_data_not_executable_code(self):
        files=list((ROOT/'inputs').glob('*'))
        self.assertGreater(len(files),3)
        self.assertTrue(all(f.suffix=='.json' for f in files))

if __name__=='__main__':
    unittest.main()
