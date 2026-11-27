"""
Compare the people layer with the people in Robert's legacy tables.

usage: compare_people.py ROOT DUMP MBOX-DIR

Reports, for pgsql-hackers in 2019-2025, where the legacy parser's
grouping of messages by sender (recomputed from data/mail with that year's
%email_map) differs from the people here; legacy %email_map entries whose
address and name are different people here; and 2016-2025 commits whose
legacy author is someone other than our primary author.
"""

import collections
import glob
import os
import re
import sys

ROOT, DUMP, MBOX_DIR = sys.argv[1:4]
sys.path.insert(0, ROOT)
import commitdata  # noqa: E402
import people      # noqa: E402

def unescape(v):
    if v == '\\N':
        return None
    return re.sub(r'\\(.)', lambda m: {'t': '\t', 'n': '\n', 'r': '\r'}.get(m[1], m[1]), v)


def dump_tables(path):
    tables, name = collections.defaultdict(list), None
    for line in open(path, encoding='utf-8', errors='replace'):
        line = line.rstrip('\n')
        m = re.match(r'COPY public\.(\w+) \(', line)
        if m:
            name = m[1]
        elif line == '\\.':
            name = None
        elif name:
            tables[name].append([unescape(v) for v in line.split('\t')])
    return tables


data = commitdata.load(ROOT)
p = people.load(ROOT, data)
tables = dump_tables(DUMP)


def who(name, address=None):
    if name and people.ADDRESS.fullmatch(name) and not address:
        name, address = None, name
    pid, status = p.resolve(name, address)
    return pid or f'?{status}:{name or address}'


def email_maps():
    maps = {}
    for parse in glob.glob(os.path.join(MBOX_DIR, 'mbox20??', 'parse')):
        year = int(re.search(r'mbox(\d{4})', parse)[1])
        maps[year] = dict(re.findall(r"'([^']+)'\s*=>\s*'([^']+)'", open(parse, encoding='utf-8').read()))
    return maps


def legacy_label(name, address, email_map):
    """The name the legacy parser filed a message under."""
    address = address.replace('(at)', '@').replace('(dot)', '.')
    if address in email_map:
        return email_map[address]
    if not name:
        return address
    m = re.fullmatch(r'(\w+), (\w+)', name)
    if m:
        name = f'{m[2]} {m[1]}'
    name = re.sub(r'\(.*\)$', '', name)
    name = re.sub(r'from 2ndquadrant', '', name, flags=re.I).strip()
    name = ' '.join(w[:1].upper() + w[1:].lower() for w in name.split())
    return email_map.get(name, name)


print('== pgsql-hackers 2019-2025: our people vs the legacy grouping')
maps = email_maps()
table = collections.defaultdict(collections.Counter)    # legacy label: {person: messages}
for year in range(2019, 2026):
    path = os.path.join(ROOT, 'data', 'mail', 'pgsql-hackers', f'{year}.tsv')
    for _, (_, _, name, address) in commitdata.read_tsv(path, people.MAIL_COLUMNS, ROOT):
        table[legacy_label(name, address, maps.get(year, {}))][who(people.clean_name(name), address)] += 1
by_person = collections.defaultdict(collections.Counter)
for label, persons in table.items():
    for person, n in persons.items():
        by_person[person][label] += n
print('legacy labels split between people here:')
for label, persons in sorted(table.items(), key=lambda kv: -sum(kv[1].values())):
    if len(persons) > 1 and sorted(persons.values())[-2] >= 3:
        print(f'   {label!r}: {dict(persons.most_common(5))}')
print('people here who were several legacy labels, by different full names:')
for person, labels in sorted(by_person.items(), key=lambda kv: -sum(kv[1].values())):
    keys = {people.name_key(l) for l, n in labels.items() if n >= 3 and people.name_key(l)}
    if len(keys) > 1:
        print(f'   {person!r}: {dict(labels.most_common(6))}')


print('\n== legacy %email_map entries that disagree')
for parse in sorted(glob.glob(os.path.join(MBOX_DIR, 'mbox20??', 'parse'))):
    for address, name in re.findall(r"'([^']+@[^']+)'\s*=>\s*'([^']+)'", open(parse, encoding='utf-8').read()):
        a, n = who(None, address), who(name)
        if a != n:
            print(f'   {parse[-11:-6]} {address} -> {name!r}: address is {a!r}, name is {n!r}')

print('\n== commits whose legacy author is someone else')
by7 = collections.defaultdict(list)
for h in data.commits:
    by7[h[:7]].append(h)
for year in range(2016, 2026):
    for row in tables[f'commits{year}']:
        commitid, author = row[0], row[5]
        hs = [h for h in by7.get(commitid[:7], []) if h.startswith(commitid) or commitid.startswith(h)]
        if len(hs) != 1 or not author:
            continue
        c = data.commits[hs[0]]
        primary = p.primary_author(c)
        ours = primary[1] if primary else None
        theirs = who(author)
        if ours and not theirs.startswith('?') and ours != theirs:
            print(f'   {c.hash} legacy {author!r} ({theirs}) ours {primary[0] if primary else None!r} ({ours})')
        elif theirs.startswith('?'):
            print(f'   {c.hash} legacy {author!r} matches no one; ours {primary[0] if primary else None!r} ({ours})')
