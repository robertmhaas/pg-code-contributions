#!/usr/bin/env python3
"""
Classify commits whose lines shouldn't be credited as written.

Writes data/line-adjustments/YYYY.tsv, one row per commit that is either
reverted or mechanical:

    hash  kind  details

where kind is "revert" or "mechanical" and details is a comma-separated
list of key=value pairs: reverted-by=HASH and revert-date=YYYY-MM-DD (UTC)
for reverts, residual=N when only part of the commit is discounted (N lines
are still credited), and rule=NAME saying which rule fired.  The file for a year holds that year's commits,
oldest first.  See README.md for the rules.

Every year is rewritten on each run, since a revert can reclassify a commit
from an earlier year.  Reads data/commits/ and, for commit messages and
diffs, a PostgreSQL clone.
"""

import argparse
import collections
import concurrent.futures
import datetime
import glob
import os
import re
import subprocess
import sys

import commitdata

ABBREV = 11
COLUMNS = ['hash', 'kind', 'details']

# Mechanical rules only look at commits at least this big; small commits
# don't distort the totals much, and do take some effort.
MIN_INSERTIONS = 300
# A residual smaller than this isn't worth recording.
MIN_RESIDUAL = 50

FORMAT_SUBJECT = re.compile(
    r'pgindent|perltidy|mechanical code beautification|^update copyrights?\b'
    r'|^translation updates|\breformat|\bre-?indent', re.I)
GENERATED_PATH = re.compile(r'''(
    \.po$ |
    ^src/backend/utils/mb/Unicode/.*\.(map|txt|xml)$ |
    ^src/include/common/unicode_\w+_table\.h$ | ^src/common/unicode/.*\.txt$ |
    ^src/timezone/data/ |
    ^src/backend/snowball/(libstemmer|stopwords)/ | ^src/include/snowball/libstemmer/ |
    ^config/config\.(guess|sub)$ |
    ^src/tools/pgindent/typedefs\.list$
)''', re.X)
# Share of a commit that must be generated files, moved lines, or
# rearranged words for the commit to count as mechanical.
GENERATED_SHARE = 0.5
MOVED_SHARE = 0.7
WORD_SHARE = 0.97

HASH = re.compile(r'\b[0-9a-f]{7,40}\b')
FOLLOW_UPS = re.compile(r'follow-?ups?|follow-?on|later commits|subsequent commits|related commits|fallout', re.I)


def git(repo, *args):
    return subprocess.run(
        ['git', '-C', repo, '-c', f'core.abbrev={ABBREV}', *args],
        capture_output=True, check=True, text=True, encoding='utf-8',
        errors='replace').stdout


def load_commits(data_dir):
    """[(hash, year, insertions, subject)] in master order, and {hash: UTC date}."""
    commits, dates = [], {}
    for path in sorted(glob.glob(os.path.join(data_dir, 'commits', '*.tsv'))):
        year = int(os.path.basename(path)[:-4])
        for _, (h, _, committed_at, insertions, subject) in commitdata.read_tsv(
                path, commitdata.COMMIT_COLUMNS, data_dir):
            commits.append((h, year, int(insertions), subject))
            dates[h] = commitdata.utc_date(committed_at)
    return commits, dates


def find_reverts(repo, ref, commits):
    """
    Return {reverted hash: (reverting hash, rule)}.

    A commit is a revert if its subject starts with "Revert" or its message
    says "This reverts commit".  Its targets are the commits it names in a
    sentence that mentions reverting, the commit whose subject it quotes
    ('Revert "..."'), and, if it says it also reverts follow-up commits, the
    commits in between that mention one of those targets by hash.  A line
    that starts with a hash, as in "The following commits are reverted:"
    and a list, names a target too.
    """
    order = {h: i for i, (h, _, _, _) in enumerate(commits)}
    by_prefix = collections.defaultdict(list)
    for h in order:
        by_prefix[h[:7]].append(h)
    by_subject = collections.defaultdict(list)
    for h, _, _, subject in commits:
        by_subject[subject.strip()].append(h)

    def resolve(x):
        found = [h for h in by_prefix.get(x[:7], [])
                 if h.startswith(x) or x.startswith(h)]
        return found[0] if len(found) == 1 else None

    messages = {}
    for record in git(repo, 'log', '--format=%h%x00%B%x1e', ref).split('\x1e'):
        record = record.strip('\n')
        if '\x00' in record:
            h, message = record.split('\x00', 1)
            if h in order:
                messages[h] = message

    def is_revert(message):
        return bool(re.match(r'\s*revert', message, re.I)
                    or re.search(r'This reverts commits?\b', message))
    reverts = {h for h, m in messages.items() if is_revert(m)}

    reverted = {}
    for h in sorted(reverts, key=order.get):
        message = messages[h]
        subject = message.split('\n', 1)[0]
        targets = {}
        # "This reverts commit X, Y and Z": these may be reverts themselves
        plain = set()
        for m in re.finditer(r'This reverts commits?((?:[\s,]+(?:and\s+)?[0-9a-f]{7,40}\b)+)', message):
            plain.update(filter(None, map(resolve, HASH.findall(m[1]))))
        for sentence in re.split(r'(?<=[.!?])\s+|\n\s*\n', message):
            if re.search(r'revert', sentence, re.I):
                for x in HASH.findall(sentence):
                    t = resolve(x)
                    # a passing mention of an earlier revert isn't a revert of it
                    if t and order[t] < order[h] and (t in plain or t not in reverts):
                        targets.setdefault(t, 'revert-message')
        # "The following commits are reverted:" and an indented list
        lines = message.split('\n')
        prev_listed = False
        for i, line in enumerate(lines[1:], 1):
            listed = re.match(r'\s*(?:[-*]\s*)?(?:commit\s+)?([0-9a-f]{7,40})\b', line)
            # a list is indented, follows a line ending in ":", or continues
            # one; a hash that a line wrap left at the start of a line isn't
            before = next((l for l in reversed(lines[:i]) if l.strip()), '')
            in_list = listed and (line[:1].isspace() or before.rstrip().endswith(':') or prev_listed)
            prev_listed = bool(in_list)
            if in_list:
                t = resolve(listed[1])
                if t and order[t] < order[h] and t not in reverts:
                    targets.setdefault(t, 'revert-list')
        quoted = re.match(r'\s*Revert\s+"(.*)"\s*$', subject)
        if quoted:
            earlier = [t for t in by_subject.get(quoted[1].strip(), [])
                       if order[t] < order[h]]
            if earlier:
                targets.setdefault(max(earlier, key=order.get), 'revert-subject')
        if targets and FOLLOW_UPS.search(message):
            first = min(order[t] for t in targets)
            short = [t[:9] for t in targets]
            for later, _, _, _ in commits[first + 1:order[h]]:
                if later not in targets and later not in reverts and any(s in messages.get(later, '') for s in short):
                    targets[later] = 'revert-follow-up'
        for t, rule in targets.items():
            reverted.setdefault(t, (h, rule))
    return reverted


def diff_facts(repo, h):
    """(generated-file insertions, other insertions, moved lines, added lines, word overlap)"""
    generated = other = 0
    for line in git(repo, 'show', '-w', '-M', '--numstat', '--format=', h).splitlines():
        added, _, path = line.split('\t', 2)
        if added == '-':
            continue
        path = re.sub(r'\{[^}]* => ([^}]*)\}', r'\1', path).split(' => ')[-1]
        if GENERATED_PATH.search(path):
            generated += int(added)
        else:
            other += int(added)

    plus_lines, minus_lines = [], collections.Counter()
    plus_words, minus_words = collections.Counter(), collections.Counter()
    for line in git(repo, 'show', '-w', '-M', '--format=', '--unified=0',
                    '--no-color', h).splitlines():
        if not line or line.startswith(('+++', '---')) or line[0] not in '+-':
            continue
        words = re.findall(r'\w+', line[1:])
        normalized = ' '.join(line[1:].split())
        if line[0] == '+':
            plus_words.update(words)
            if len(normalized) > 3:
                plus_lines.append(normalized)
        else:
            minus_words.update(words)
            if len(normalized) > 3:
                minus_lines[normalized] += 1
    moved = 0
    for line in plus_lines:
        if minus_lines[line] > 0:
            minus_lines[line] -= 1
            moved += 1
    total_words = sum(plus_words.values())
    common_words = sum(min(n, minus_words[w]) for w, n in plus_words.items())
    overlap = common_words / total_words if total_words else 0
    return generated, other, moved, len(plus_lines), overlap


def mechanical(insertions, subject, facts):
    """(residual, rule) if the commit is mechanical, else None."""
    generated, other, moved, added, overlap = facts
    if FORMAT_SUBJECT.search(subject):
        return 0, 'format-subject'
    if generated and generated >= GENERATED_SHARE * (generated + other):
        return (other if other >= MIN_RESIDUAL else 0), 'generated-files'
    if added and moved >= MOVED_SHARE * added:
        new = round(insertions * (added - moved) / added)
        return (new if new >= MIN_RESIDUAL else 0), 'code-motion'
    if overlap >= WORD_SHARE:
        return 0, 'reformat'
    return None


def write_tsv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\t'.join(COLUMNS) + '\n')
        for row in rows:
            f.write('\t'.join(row) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().split('\n')[0])
    parser.add_argument('--repo', required=True, help='path to a PostgreSQL clone')
    parser.add_argument('--ref', default='master', help='branch to read (default: master)')
    parser.add_argument('--data', default='data', help='data directory (default: data)')
    args = parser.parse_args()

    commits, dates = load_commits(args.data)
    reverted = find_reverts(args.repo, args.ref, commits)
    reverting = {r for r, _ in reverted.values()}

    candidates = [(h, ins, subj) for h, _, ins, subj in commits
                  if ins >= MIN_INSERTIONS and h not in reverted and h not in reverting]
    with concurrent.futures.ThreadPoolExecutor(os.cpu_count()) as pool:
        facts = dict(zip((h for h, _, _ in candidates),
                         pool.map(lambda c: diff_facts(args.repo, c[0]), candidates)))

    rows = collections.defaultdict(list)
    counts = collections.Counter()
    for h, year, insertions, subject in commits:
        if h in reverted:
            by, rule = reverted[h]
            rows[year].append([h, 'revert', f'reverted-by={by},revert-date={dates[by]},rule={rule}'])
            counts[rule] += 1
        elif h in facts:
            m = mechanical(insertions, subject, facts[h])
            if m:
                residual, rule = m
                details = (f'residual={residual},' if residual else '') + f'rule={rule}'
                rows[year].append([h, 'mechanical', details])
                counts[rule] += 1

    out_dir = os.path.join(args.data, 'line-adjustments')
    for year, year_rows in rows.items():
        write_tsv(os.path.join(out_dir, f'{year}.tsv'), year_rows)
    for path in glob.glob(os.path.join(out_dir, '*.tsv')):
        if int(os.path.basename(path)[:-4]) not in rows:
            os.remove(path)
    print(f'{len(reverting)} reverts of {len(reverted)} commits; '
          f'{sum(counts.values())} rows: {dict(counts)}', file=sys.stderr)


if __name__ == '__main__':
    main()
