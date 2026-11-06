"""Generate docs/decisions.md: every setting in config/pipeline.yml with its value and comment.

    pixi run python scripts/document_config.py          # rewrite docs/decisions.md
    pixi run python scripts/document_config.py --check  # exit 1 if it is out of date (run by the tests)

The table is built from the YAML file itself (keys, values and trailing
comments), so the documentation of each choice cannot drift from the value used.
"""

import argparse
import re
import sys

from sm_drydown.config import CONFIG_DIR, REPO_ROOT

TARGET = REPO_ROOT / 'docs' / 'decisions.md'
HEADER = """# Processing decisions

Generated from [config/pipeline.yml](../config/pipeline.yml) by `scripts/document_config.py`; do not edit.
Every value below is used as written; a run file in [config/runs/](../config/runs/) overrides only the keys
it lists, and each run's resolved settings are saved in `output/runs/<run>/config.resolved.yml`.
Units are in brackets; `[-]` is dimensionless.
"""


def rows(text):
    """(section, dotted key, value, comment) for every setting; list items get [i] indices."""
    section, parents, out, notes, counters = None, [], [], {}, {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or (stripped.startswith('#') and section is None):
            continue
        if stripped.startswith('#'):
            notes.setdefault(section, []).append(stripped.lstrip('# '))
            continue
        indent = len(line) - len(line.lstrip())
        body, _, comment = line.partition(' #')
        body, comment = body.strip(), comment.strip()
        if indent == 0:
            section, parents = body.rstrip(':'), []
            continue
        level = (indent - 2) // 2
        if body.startswith('- '):
            container = '.'.join(parents[:level])
            index = counters[container] = counters.get(container, -1) + 1 if counters.get('_last') == container else 0
            counters['_last'] = container
            item = body[2:]
            if ':' not in item:
                out.append((section, f'{container}[{index}]' if container else f'[{index}]', item, comment))
                continue
            parents = parents[:level] + [f'[{index}]']
            key, _, value = item.partition(':')
            parents.append(key.strip())
            out.append((section, '.'.join(parents).replace('.[', '['), value.strip(), comment))
            continue
        key, _, value = body.partition(':')
        parents = parents[:level] + [key.strip()]
        if value.strip() or comment:
            out.append((section, '.'.join(parents).replace('.[', '['), value.strip(), comment))
    return out, notes


def render():
    entries, notes = rows((CONFIG_DIR / 'pipeline.yml').read_text())
    text, current = [HEADER], None
    for section, key, value, comment in entries:
        if section != current:
            current = section
            text.append(f'\n## `{section}`\n')
            if notes.get(section):
                text.append(' '.join(notes[section]) + '\n')
            text.append('| Setting | Value | Meaning |\n| --- | --- | --- |')
        text.append(f"| `{key}` | {'`' + value + '`' if value else ''} | {comment.replace('|', '/')} |")
    return '\n'.join(text) + '\n'


parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
if args.check:
    if not TARGET.exists() or TARGET.read_text() != render():
        sys.exit('docs/decisions.md is out of date: run `pixi run python scripts/document_config.py`')
else:
    TARGET.write_text(render())
    print(f'Wrote {TARGET.relative_to(REPO_ROOT)}')
