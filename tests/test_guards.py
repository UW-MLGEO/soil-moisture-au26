"""Guards that keep every data-affecting choice in config/pipeline.yml, documented.

1. No numeric literal with scientific meaning in the code that turns raw values
   into features, checks or exports: only structural values (indices, counts,
   unit conversions, hash-prefix lengths) are allowed, or a line marked
   ``# not-science: <reason>`` (file-format facts, console output, layout). Provider clients
   (src/sm_drydown/providers/) are exempt: their numbers are transfer limits,
   timeouts and provider grid definitions, not processing choices.
2. Every leaf key in pipeline.yml carries a comment with a unit ([...]).
3. docs/decisions.md is regenerated from pipeline.yml (it cannot drift).
4. Every feature listed in pipeline.yml has a builder, a table schema and a level.
"""
import ast
import re
import unittest
from pathlib import Path

from sm_drydown.config import CONFIG_DIR, REPO_ROOT, load_config
from sm_drydown.features import FEATURES, GROUPS
from sm_drydown.schemas import SCHEMAS

ALLOWED = {0, 1, 2, -1,
           12, 16,    # lengths of hex hash prefixes (collection IDs and log output; sensor IDs)
           100,       # unit conversion: percent -> fraction
           273.15,    # unit conversion: K -> degC
           1e-12}     # float64 guard at an exact decision boundary (inspection.checks.FLOAT_TIE)
LAYOUT_KEYWORDS = {'figsize', 'dpi', 'fontsize'}
SCOPE = sorted([*(REPO_ROOT / 'src/sm_drydown').glob('*.py'), *(REPO_ROOT / 'src/sm_drydown/features').glob('*.py'),
                *(REPO_ROOT / 'src/sm_drydown/inspection').glob('*.py'),
                *(REPO_ROOT / 'src/sm_drydown/formats').glob('*.py'), *(REPO_ROOT / 'scripts').glob('*.py')])


class GuardTests(unittest.TestCase):
    def test_no_scientific_numbers_in_processing_code(self):
        for path in SCOPE:
            with self.subTest(path=path.relative_to(REPO_ROOT).as_posix()):
                source = path.read_text()
                lines = source.splitlines()
                tree = ast.parse(source)
                layout = {id(n) for kw in ast.walk(tree) if isinstance(kw, ast.keyword) and kw.arg in LAYOUT_KEYWORDS
                          for n in ast.walk(kw.value)}
                bad = [(node.lineno, node.value) for node in ast.walk(tree)
                       if isinstance(node, ast.Constant) and type(node.value) in (int, float)
                       and node.value not in ALLOWED and id(node) not in layout
                       and '# not-science' not in lines[node.lineno - 1]]
                self.assertFalse(bad, f'numeric literals {bad}: move them to config/pipeline.yml')

    def test_every_pipeline_setting_is_documented_with_a_unit(self):
        undocumented = []
        for number, line in enumerate((CONFIG_DIR / 'pipeline.yml').read_text().splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith(('#', '- ')) or stripped.endswith(':'):
                continue
            if re.match(r'^[A-Za-z_][\w]*:\s*\S', stripped) and not re.search(r'#.*\[[^\]]+\]', line):
                undocumented.append((number, stripped))
        self.assertFalse(undocumented, f'pipeline.yml keys without a "# [unit] ..." comment: {undocumented}')

    def test_decisions_doc_matches_pipeline_yml(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, 'scripts/document_config.py', '--check'], cwd=REPO_ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_listed_feature_has_a_builder_schema_and_level(self):
        cfg = load_config('baseline')
        for name in cfg.features:
            with self.subTest(feature=name):
                feature = FEATURES[name]
                self.assertIn(feature.group, GROUPS)
                self.assertIn(feature.table, GROUPS[feature.group].tables)
                self.assertIn(feature.table, SCHEMAS)
                self.assertIn(name, SCHEMAS[feature.table]['columns'])
                self.assertIn(feature.level, ('sensor', 'station_day', 'static', 'standalone'))


if __name__ == '__main__':
    unittest.main()
