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

Only new commits can change how an old commit is classified, by reverting
it, so a run looks only at commits added since the last one (recorded in
data/classify-state.tsv), and at what new reverts revert.  It reclassifies
everything if the rules have changed (this script's contents), with --all,
or if there's no record of a previous run.  Reads data/commits/ and, for
commit messages and diffs, a PostgreSQL clone.
"""

import argparse
import collections
import concurrent.futures
import datetime
import glob
import hashlib
import os
import re
import subprocess
import sys

import commitdata

ABBREV = 11
COLUMNS = ['hash', 'kind', 'details']
STATE_FILE = 'classify-state.tsv'
STATE_COLUMNS = ['key', 'value']

# Mechanical rules only look at commits at least this big; small commits
# don't distort the totals much, and do take some effort.
MIN_INSERTIONS = 300
# A residual smaller than this isn't worth recording.
MIN_RESIDUAL = 50
# A revert that deletes less than this share of what its targets inserted
# only reverted part of them, so they aren't treated as reverted.
PARTIAL_REVERT_SHARE = 0.3

FORMAT_SUBJECT = re.compile(
    r'pgindent|perltidy|mechanical code beautification|^update copyrights?\b'
    r'|^translation updates|\breformat|\bre-?indent', re.I)
GENERATED_PATH = re.compile(r'''(
    \.po$ |
    ^src/backend/utils/mb/Unicode/.*\.(map|txt|xml)$ |
    ^src/include/common/unicode_\w+\.h$ | ^src/common/unicode/.*\.txt$ |
    ^src/timezone/data/ |
    ^src/backend/snowball/(libstemmer|stopwords)/ | ^src/include/snowball/libstemmer/ |
    ^config/config\.(guess|sub)$ | ^configure$ | ^src/include/pg_config\.h\.in$ |
    ^src/tools/pgindent/typedefs\.list$
)''', re.X)
# A commit is mechanical if at least this share of its insertions is in
# generated files, in files whose words are almost all rearranged
# (WORD_SHARE), or moved from elsewhere in the commit.
MECHANICAL_SHARE = 0.5
WORD_SHARE = 0.97

# A commit that touches only these is a translation update: mechanical at
# any size.
TRANSLATION_PATH = re.compile(r'\.po$|(^|/)nls\.mk$|(^|/)po/LINGUAS$')

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


def find_reverts(repo, ref, commits, only=None):
    """
    Return {reverting hash: {reverted hash: rule}}, for every revert, or
    for the reverts in `only`.  (Every revert's message is still read, since
    a revert of a revert depends on what that revert reverted.)

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

    def resolve(x, before):
        """The one commit older than `before` that x abbreviates, if any."""
        found = [h for h in by_prefix.get(x[:7], [])
                 if (h.startswith(x) or x.startswith(h)) and order[h] < before]
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

    by_revert = {}
    for h in sorted(reverts, key=order.get):
        message = messages[h]
        subject = message.split('\n', 1)[0]
        targets = {}
        # "This reverts commit X, Y and Z": these may be reverts themselves
        plain = set()
        for m in re.finditer(r'This reverts commits?((?:[\s,]+(?:and\s+)?[0-9a-f]{7,40}\b)+)', message):
            plain.update(filter(None, (resolve(x, order[h]) for x in HASH.findall(m[1]))))
        for sentence in re.split(r'(?<=[.!?])\s+|\n\s*\n', message):
            if re.search(r'revert', sentence, re.I):
                for x in HASH.findall(sentence):
                    t = resolve(x, order[h])
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
                t = resolve(listed[1], order[h])
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
        if targets:
            by_revert[h] = targets
    # Reverting a revert puts back what it reverted, so those commits aren't
    # targets too, even when the message mentions them.
    for h, targets in by_revert.items():
        restored = set()
        for t in targets:
            restored.update(by_revert.get(t, {}))
        for t in restored:
            targets.pop(t, None)
    return {h: ts for h, ts in by_revert.items()
            if ts and (only is None or h in only)}


def deletions(repo, h):
    stat = git(repo, 'show', '-w', '-M', '--shortstat', '--format=', h)
    m = re.search(r'(\d+) deletions?', stat)
    return int(m[1]) if m else 0


def drop_partial_reverts(repo, by_revert, insertions):
    """
    Remove reverts that delete less than PARTIAL_REVERT_SHARE of what their
    targets inserted ("Revert part of commit X").  Returns how many there
    were.
    """
    with concurrent.futures.ThreadPoolExecutor(os.cpu_count()) as pool:
        deleted = dict(zip(by_revert, pool.map(lambda r: deletions(repo, r), by_revert)))
    partial = [r for r, ts in by_revert.items()
               if sum(insertions[t] for t in ts) >= 100
               and deleted[r] < PARTIAL_REVERT_SHARE * sum(insertions[t] for t in ts)]
    for r in partial:
        del by_revert[r]
    return len(partial)


def diff_facts(repo, h):
    """
    Per-file facts for a commit: [(path, added, generated, word overlap,
    moved)], where added is the file's insertions, word overlap is the share
    of its added words that match words it deleted, and moved is how many of
    its added lines match lines deleted anywhere in the commit (lines of
    three characters or less don't count).
    """
    added = {}
    for line in git(repo, 'show', '-w', '-M', '--numstat', '--format=', h).splitlines():
        n, _, path = line.split('\t', 2)
        if n != '-':
            added[re.sub(r'\{[^}]* => ([^}]*)\}', r'\1', path).split(' => ')[-1]] = int(n)

    plus_lines = collections.defaultdict(list)
    minus_lines = collections.Counter()
    plus_words = collections.defaultdict(collections.Counter)
    minus_words = collections.defaultdict(collections.Counter)
    path = None
    for line in git(repo, 'show', '-w', '-M', '--format=', '--unified=0',
                    '--no-color', h).splitlines():
        if line.startswith('+++ '):
            path = line[6:] if line.startswith('+++ b/') else path
            continue
        if line.startswith('--- '):
            if line.startswith('--- a/'):
                path = line[6:]
            continue
        if not line or line[0] not in '+-' or path is None:
            continue
        words = re.findall(r'\w+', line[1:])
        normalized = ' '.join(line[1:].split())
        if line[0] == '+':
            plus_words[path].update(words)
            if len(normalized) > 3:
                plus_lines[path].append(normalized)
        else:
            minus_words[path].update(words)
            if len(normalized) > 3:
                minus_lines[normalized] += 1

    facts = []
    for path, n in added.items():
        moved = 0
        for line in plus_lines[path]:
            if minus_lines[line] > 0:
                minus_lines[line] -= 1
                moved += 1
        # scale up to the file's insertions, which include the short lines
        if plus_lines[path]:
            moved = round(n * moved / len(plus_lines[path]))
        total = sum(plus_words[path].values())
        common = sum(min(c, minus_words[path][w]) for w, c in plus_words[path].items())
        facts.append((path, n, bool(GENERATED_PATH.search(path)),
                      common / total if total else 0, moved))
    return facts


def mechanical(insertions, subject, facts):
    """
    (residual, rule) if the commit is mechanical, else None.

    Each file's mechanical lines are all of them if it's a generated file
    or its words are almost all rearranged (a reformat, or a bulk edit such
    as adding a column to a catalog .dat file), and otherwise just the lines
    moved from elsewhere in the commit.  The rest is credited as the
    residual.
    """
    if FORMAT_SUBJECT.search(subject):
        return 0, 'format-subject'
    kinds = collections.Counter()
    for path, n, generated, overlap, moved in facts:
        if generated:
            kinds['generated-files'] += n
        elif overlap >= WORD_SHARE:
            kinds['reformat'] += n
        else:
            kinds['code-motion'] += moved
    discounted = sum(kinds.values())
    if not kinds or discounted < MECHANICAL_SHARE * insertions:
        return None
    residual = max(0, insertions - discounted)
    return (residual if residual >= MIN_RESIDUAL else 0), kinds.most_common(1)[0][0]


def translation_only(repo, ref):
    """Hashes of commits whose changed files are all translation files."""
    files = collections.defaultdict(list)
    h = None
    for line in git(repo, 'log', '--format=@%h', '--name-only', ref).splitlines():
        if line.startswith('@'):
            h = line[1:]
        elif line:
            files[h].append(line)
    return {h for h, fs in files.items()
            if fs and all(TRANSLATION_PATH.search(f) for f in fs)}


def write_tsv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\t'.join(COLUMNS) + '\n')
        for row in rows:
            f.write('\t'.join(row) + '\n')


def script_digest():
    with open(os.path.abspath(__file__), 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def read_state(path):
    try:
        return dict(fields for _, fields in commitdata.read_tsv(
            path, STATE_COLUMNS, os.path.dirname(path)))
    except FileNotFoundError:
        return {}


def read_rows(out_dir):
    rows = {}
    for path in glob.glob(os.path.join(out_dir, '*.tsv')):
        for _, row in commitdata.read_tsv(path, COLUMNS, out_dir):
            rows[row[0]] = row
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().split('\n')[0])
    parser.add_argument('--repo', required=True, help='path to a PostgreSQL clone')
    parser.add_argument('--ref', default='master', help='branch to read (default: master)')
    parser.add_argument('--data', default='data', help='data directory (default: data)')
    parser.add_argument('--all', action='store_true',
                        help='reclassify every commit, not just new ones')
    parser.add_argument('--until', metavar='HASH',
                        help='classify history only up to this commit (for testing)')
    args = parser.parse_args()

    commits, dates = load_commits(args.data)
    if args.until:
        commits = commits[:[c[0] for c in commits].index(args.until) + 1]
    order = {h: i for i, (h, _, _, _) in enumerate(commits)}
    out_dir = os.path.join(args.data, 'line-adjustments')
    state_path = os.path.join(args.data, STATE_FILE)

    # Only new commits can change the classification of old ones, by
    # reverting them, unless the rules have changed.
    state = read_state(state_path)
    full = (args.all or state.get('script') != script_digest()
            or state.get('through') not in order)
    if full:
        rows, start = {}, 0
    else:
        rows, start = read_rows(out_dir), order[state['through']] + 1
    new = commits[start:]
    new_hashes = {h for h, _, _, _ in new}

    by_revert = find_reverts(args.repo, args.ref, commits, None if full else new_hashes)
    partial = drop_partial_reverts(args.repo, by_revert,
                                   {h: ins for h, _, ins, _ in commits})
    counts = collections.Counter()
    for r in sorted(by_revert, key=order.get):
        for t, rule in by_revert[r].items():
            if t in rows and rows[t][1] == 'revert':
                continue            # the earliest revert is the one that counts
            rows[t] = [t, 'revert', f'reverted-by={r},revert-date={dates[r]},rule={rule}']
            counts[rule] += 1
    reverting = {commitdata.parse_details(row[2]).get('reverted-by')
                 for row in rows.values() if row[1] == 'revert'}

    translations = translation_only(args.repo, args.ref) & new_hashes
    candidates = [(h, ins) for h, _, ins, _ in new
                  if ins >= MIN_INSERTIONS and h not in rows and h not in reverting
                  and h not in translations]
    with concurrent.futures.ThreadPoolExecutor(os.cpu_count()) as pool:
        facts = dict(zip((h for h, _ in candidates),
                         pool.map(lambda c: diff_facts(args.repo, c[0]), candidates)))
    for h, _, insertions, subject in new:
        if h in rows or h in reverting:
            continue
        if h in translations and insertions:
            rows[h] = [h, 'mechanical', 'rule=translation']
            counts['translation'] += 1
        elif h in facts:
            m = mechanical(insertions, subject, facts[h])
            if m:
                residual, rule = m
                details = (f'residual={residual},' if residual else '') + f'rule={rule}'
                rows[h] = [h, 'mechanical', details]
                counts[rule] += 1

    by_year = collections.defaultdict(list)
    year_of = {h: y for h, y, _, _ in commits}
    for h in sorted((h for h in rows if h in order), key=order.get):
        by_year[year_of[h]].append(rows[h])
    for year, year_rows in by_year.items():
        write_tsv(os.path.join(out_dir, f'{year}.tsv'), year_rows)
    for path in glob.glob(os.path.join(out_dir, '*.tsv')):
        if int(os.path.basename(path)[:-4]) not in by_year:
            os.remove(path)
    with open(state_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('key\tvalue\n')
        f.write(f'through\t{commits[-1][0]}\n')
        f.write(f'script\t{script_digest()}\n')
    print(f"{'full' if full else 'incremental'}: {len(new)} commits classified, "
          f"{len(by_revert)} reverts found ({partial} partial, ignored); "
          f"new rows: {dict(counts)}", file=sys.stderr)


if __name__ == '__main__':
    main()
