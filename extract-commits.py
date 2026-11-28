#!/usr/bin/env python3
"""
Extract per-commit metadata from a PostgreSQL git repository.

    python3 extract-commits.py --repo ~/pgsql --ref origin/master          # changed years
    python3 extract-commits.py --repo ~/pgsql --ref origin/master --all    # everything

For every year, writes

    data/commits/YYYY.tsv   hash  author  committed_at  insertions  subject
    data/tags/YYYY.tsv      hash  tag  value

in PostgreSQL COPY text format, rows in master order, oldest first.

- hash is %h, pinned to 11 digits (longer only if ambiguous).
- author is %an, the git author.
- committed_at is %ci, the committer date as recorded, with its offset.
- insertions is from git log --shortstat -w -M; a commit that only
  changes whitespace has no stat line and gets 0.
- subject is %s.  Some older commits have no blank line after the first
  line, so their subject runs on into the rest of that paragraph.
- tag and value are a structured tag's key and the rest of its line, both
  as typed, one row per tag, in message order.

Every commit reachable from the ref is included.  A commit belongs to the
UTC year of its committer date, except that it's never dated earlier than
the commit before it, which guards against skewed clocks and author dates
carried over as committer dates.  So a few commits sit in a file whose
year differs from the date in committed_at: 2019-01-01 08:53:02 +0900 is
in 2018.

A tag is any line "Key: value" except the first line.  The key is a word
or hyphenated words (Author, Reviewed-by, Backpatch-through) or, for older
commits, a phrase of up to four words ending in "by" (Submitted by,
Pointed out by).  Spaces may come before the colon, and at least one space
or tab must follow it, which rules out URLs and times.  An indented line
right after a tag continues its value, as with a Discussion: tag followed
by a list of URLs.  A tag with no value is dropped.  Prose such as "Patch
by Jane Doe, reviewed by John Roe" isn't captured, and lines that only
look like tags, such as "ERROR: ..." pasted from psql, are; tag fixes deal
with the first, and readers pick tags by key.

By default only years whose list of commits has changed are rewritten,
which in practice means the current year, and the previous one around New
Year.  --all rewrites every year, in parallel; that takes about 40 seconds
on an 8-core laptop.  Use an up-to-date clone: a stale one rewrites the
current year without the commits it's missing.
"""

import argparse
import concurrent.futures
import datetime
import os
import re
import subprocess
import sys

# Pin the abbreviation length so %h doesn't depend on the clone's object
# count.  git still lengthens a hash if 11 digits would be ambiguous.
ABBREV = 11

COMMIT_COLUMNS = ['hash', 'author', 'committed_at', 'insertions', 'subject']
TAG_COLUMNS = ['hash', 'tag', 'value']

# A tag is "Key: value" at the start of a line.  The key is a word or
# hyphenated words (Reviewed-by, Discussion), or, for older commits, a short
# phrase ending in "by" (Reviewed by, Pointed out by).  The colon must be
# followed by whitespace or end of line, which excludes URLs and times.
TAG_RE = re.compile(r'''
    ( [A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*
    | [A-Za-z]+(?:\ [A-Za-z]+){0,2}\ by )
    [ \t]*:
    (?: [ \t]+ (.*) )?
    $''', re.X | re.I)

INSERTIONS_RE = re.compile(r'(\d+) insertions?\(\+\)')

# Field and record separators for git log output; asserted absent from
# commit messages below.
FS, RS = '\x1f', '\x1e'


def git(repo, *args, input=None):
    return subprocess.run(
        ['git', '-C', repo, '-c', f'core.abbrev={ABBREV}', *args],
        input=input, capture_output=True, check=True,
        text=True, encoding='utf-8', errors='replace').stdout


def list_commits(repo, ref):
    """
    Return [(full_hash, hash, year)] for every commit on ref, oldest first.

    The year is the UTC year of the committer date, except that a commit is
    never dated earlier than the commit before it: history on master is
    linear and in push order, so an earlier date (from a skewed clock or a
    preserved author date) can't be when it actually landed.
    """
    out = git(repo, 'log', '--reverse', '--topo-order', '--format=%H %h %ct', ref)
    commits = []
    floor = 0
    for line in out.splitlines():
        full_hash, h, ct = line.split()
        floor = max(floor, int(ct))
        year = datetime.datetime.fromtimestamp(floor, datetime.timezone.utc).year
        commits.append((full_hash, h, year))
    return commits


def parse_tags(message):
    """
    Return [(tag, value)] in message order.

    Every line but the first is scanned, since older commits often have no
    blank line after the subject.  An indented line directly after a tag
    continues its value.  Tags with no value are dropped.
    """
    tags = []
    open_tag = False
    for line in message.split('\n')[1:]:
        if open_tag and line[:1] in (' ', '\t') and line.strip():
            tag, value = tags[-1]
            tags[-1] = (tag, f'{value} {line.strip()}'.strip())
            continue
        m = TAG_RE.match(line)
        open_tag = m is not None
        if m:
            tags.append((m.group(1), (m.group(2) or '').strip()))
    return [(tag, value) for tag, value in tags if value]


def extract(repo, full_hashes):
    """Return (commit_rows, tag_rows) for the given commits, in that order."""
    out = git(repo, 'log', '--no-walk=unsorted', '--stdin',
              '--shortstat', '-w', '-M',
              f'--format={RS}%h{FS}%an{FS}%ci{FS}%s{FS}%B{FS}',
              input=''.join(h + '\n' for h in full_hashes))
    commit_rows = []
    tag_rows = []
    for record in out.split(RS)[1:]:
        fields = record.split(FS)
        if len(fields) != 6:
            raise RuntimeError(f'unexpected git log output: {record[:200]!r}')
        h, author, committed_at, subject, message, stat = fields
        m = INSERTIONS_RE.search(stat)
        insertions = m.group(1) if m else '0'
        commit_rows.append([h, author, committed_at, insertions, subject])
        tag_rows.extend([h, tag, value] for tag, value in parse_tags(message))
    if len(commit_rows) != len(full_hashes):
        raise RuntimeError(f'asked for {len(full_hashes)} commits, '
                           f'got {len(commit_rows)}')
    return commit_rows, tag_rows


def escape(value):
    return (value.replace('\\', '\\\\').replace('\t', '\\t')
            .replace('\n', '\\n').replace('\r', '\\r'))


def write_tsv(path, columns, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\t'.join(columns) + '\n')
        for row in rows:
            f.write('\t'.join(escape(v) for v in row) + '\n')


def recorded_hashes(path):
    """Return the hash column of an existing commits file, or None."""
    try:
        with open(path, encoding='utf-8') as f:
            next(f)
            return [line.split('\t', 1)[0] for line in f]
    except FileNotFoundError:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().split('\n')[0])
    parser.add_argument('--repo', required=True,
                        help='path to a PostgreSQL clone')
    parser.add_argument('--ref', default='master',
                        help='branch to read (default: master)')
    parser.add_argument('--data', default='data',
                        help='output directory (default: data)')
    parser.add_argument('--all', action='store_true',
                        help='rewrite every year, not just changed ones')
    args = parser.parse_args()

    commits = list_commits(args.repo, args.ref)
    by_year = {}
    for full_hash, h, year in commits:
        by_year.setdefault(year, []).append((full_hash, h))

    todo = []
    for year, year_commits in sorted(by_year.items()):
        path = os.path.join(args.data, 'commits', f'{year}.tsv')
        if args.all or recorded_hashes(path) != [h for _, h in year_commits]:
            todo.append(year)

    with concurrent.futures.ThreadPoolExecutor(os.cpu_count()) as pool:
        results = pool.map(
            lambda y: extract(args.repo, [f for f, _ in by_year[y]]), todo)
        for year, (commit_rows, tag_rows) in zip(todo, results):
            write_tsv(os.path.join(args.data, 'commits', f'{year}.tsv'),
                      COMMIT_COLUMNS, commit_rows)
            write_tsv(os.path.join(args.data, 'tags', f'{year}.tsv'),
                      TAG_COLUMNS, tag_rows)
            print(f'{year}: {len(commit_rows)} commits, {len(tag_rows)} tags',
                  file=sys.stderr)

    # Remove files for years that no longer have any commits, e.g. after a
    # change to the year rule.
    for kind in ('commits', 'tags'):
        directory = os.path.join(args.data, kind)
        for name in os.listdir(directory) if os.path.isdir(directory) else []:
            if name.endswith('.tsv') and int(name[:-4]) not in by_year:
                os.remove(os.path.join(directory, name))
                print(f'removed {kind}/{name}', file=sys.stderr)

    if not todo:
        print('no changes', file=sys.stderr)


if __name__ == '__main__':
    main()
