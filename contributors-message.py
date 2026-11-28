#!/usr/bin/env python3
"""
Print a commit message for changes to data/recognized-contributors.tsv.

Compares the file as committed (HEAD) with the working tree.  A person in
both is matched by username, or by name if they have no username on one
side.  The subject counts people added and people whose section changed:

    Add 2 new contributors and recategorize 1 contributor

and the body has a line for each, added people first, in page order:

    Congratulations to <name>, added as a Significant Contributor
    Congratulations to <name>, now on the Core Team
    Congratulations to <name>, now a Major Contributor
    Thanks to <name>, now a Past Major Contributor

A move to the Core Team, or to Major Contributors from anywhere but the
Core Team, gets "Congratulations"; any other move gets "Thanks".  When
nobody was added or moved (an address changed, say, or someone was
removed), the subject is just "Update recognized contributors", and
when the file isn't committed yet, "Add recognized contributors".
"""

import os
import subprocess

import commitdata

PATH = 'data/recognized-contributors.tsv'
CORE = 'Core Team'
MAJOR = 'Major Contributors'


def parse(text):
    """[(section, username, name)] from the file's text, in file order."""
    rows = []
    for line in text.splitlines()[1:]:
        section, username, name, _ = map(commitdata.unescape, line.split('\t'))
        rows.append((section, username, name))
    return rows


def committed():
    result = subprocess.run(['git', 'show', 'HEAD:' + PATH],
                            cwd=commitdata.ROOT, capture_output=True,
                            text=True, encoding='utf-8')
    return result.stdout if result.returncode == 0 else ''


def match(old, new):
    """{index in new: index in old} for people in both."""
    pairs = {}
    by_username = {r[1]: i for i, r in enumerate(old) if r[1]}
    for j, r in enumerate(new):
        if r[1] in by_username:
            pairs[j] = by_username.pop(r[1])
    matched = set(pairs.values())
    by_name = {}
    for i, r in enumerate(old):
        if i not in matched:
            by_name.setdefault(r[2], i)
    for j, r in enumerate(new):
        if j not in pairs and r[2] in by_name:
            pairs[j] = by_name.pop(r[2])
    return pairs


def one(section):
    """"a Major Contributor" for "Major Contributors"."""
    if section == CORE:
        return 'on the Core Team'
    return 'a ' + section.removesuffix('s')


def contributors(n):
    return f'{n} contributor' + ('' if n == 1 else 's')


def message(old, new):
    pairs = match(old, new)
    added, moved = [], []
    for j, (section, _, name) in enumerate(new):
        if j not in pairs:
            where = 'to the Core Team' if section == CORE else 'as ' + one(section)
            added.append(f'Congratulations to {name}, added {where}')
        elif old[pairs[j]][0] != section:
            before = old[pairs[j]][0]
            if section == CORE or (section == MAJOR and before != CORE):
                moved.append(f'Congratulations to {name}, now {one(section)}')
            else:
                moved.append(f'Thanks to {name}, now {one(section)}')

    parts = []
    if added:
        parts.append('add ' + contributors(len(added)).replace(' ', ' new ', 1))
    if moved:
        parts.append('recategorize ' + contributors(len(moved)))
    if not parts:
        return 'Update recognized contributors\n'
    subject = ' and '.join(parts)
    return subject[0].upper() + subject[1:] + '\n\n' + '\n'.join(added + moved) + '\n'


def main():
    with open(os.path.join(commitdata.ROOT, PATH), encoding='utf-8') as f:
        new = parse(f.read())
    old = committed()
    print(message(parse(old), new) if old else 'Add recognized contributors\n',
          end='')


if __name__ == '__main__':
    main()
