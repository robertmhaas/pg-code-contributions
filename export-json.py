#!/usr/bin/env python3
"""
Write the yearly report figures as JSON.

    python3 export-json.py -o contributions.json

The same figures as export-db.py's top_authors, top_opp_committers and
top_senders views, with every row, and what the data covers: the latest
commit, and which months of each list's archive were read.  Leaving rows
out, and saying so, is up to whatever shows them.

    {"generated": ..., "latest_commit": {"hash": ..., "committed_at": ...},
     "mail": {"pgsql-hackers": {"months": ["2016-01", ...]}},
     "years": {"2025": {
       "commits": {"count", "first", "last", "complete",
                   "authors": [{"person", "name", "lines", "commits", "committer"}],
                   "for_others": [{"person", "name", "lines", "commits"}]},
       "mail": {"pgsql-hackers": {"count", "months", "complete",
                                  "senders": [{"person", "name", "messages"}]}}}}}

- authors is top_authors, for_others is top_opp_committers, and senders is
  top_senders, without the percentages, which are left to whatever shows
  them.  committer is false where top_authors shows [*].
- person is the person id, or null for "(no one)" and "(unidentified)".
  Since it's a Canonical: value, it includes the person's address if they
  have one.
- complete says whether the year is over: for commits, whether there are
  commits from a later year; for mail, whether all twelve months were
  read.  A mail count leaves out messages from what isn't a person, as
  senders does.
"""

import argparse
import collections
import datetime
import glob
import json
import os
import sys

import commitdata
import people

MONTH_COLUMNS = ['month']


def ranked(counts, key, names):
    """
    Rows for {person id or None: {key: n, ...}}, most first, ties broken
    by the rest of the counts and then by name.
    """
    rows = [{'person': pid, 'name': names(pid), **c} for pid, c in counts.items()]
    rows.sort(key=lambda r: ([-r[k] for k in key], r['name']))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().split('\n')[0])
    parser.add_argument('-o', '--output', help='file to write (default: standard output)')
    args = parser.parse_args()

    root = commitdata.ROOT
    data = commitdata.load(root)
    p = people.load(root)
    if p.problems:
        for problem in p.problems:
            print(problem, file=sys.stderr)
        sys.exit('people.txt has problems; run check-people.py')

    def name(pid, nobody):
        return p.persons[pid].name if pid else nobody

    years = collections.defaultdict(dict)
    authors = collections.defaultdict(lambda: collections.defaultdict(
        lambda: {'lines': 0, 'commits': 0}))
    for_others = collections.defaultdict(lambda: collections.defaultdict(
        lambda: {'lines': 0, 'commits': 0}))
    span = {}
    committer_since = {}
    for c in data.commits.values():
        committer, _ = p.resolve(people.clean_name(c.git_author))
        primary = p.primary_author(c)
        author = primary[1] if primary else None
        lines = data.credited_lines(c, reverts_until=datetime.date(c.year, 12, 31))
        a = authors[c.year][author]
        a['lines'] += lines
        a['commits'] += 1
        # As in top_opp_committers, a commit whose committer or primary
        # author can't be identified isn't counted as for someone else.
        if committer and author and committer != author:
            o = for_others[c.year][committer]
            o['lines'] += lines
            o['commits'] += 1
        if committer:
            committer_since.setdefault(committer, c.year)
        first, last, n = span.get(c.year, (c, c, 0))
        span[c.year] = (first, c, n + 1)
    latest = next(reversed(data.commits.values()))

    for year, (first, last, n) in span.items():
        rows = ranked(authors[year], ['lines', 'commits'],
                      lambda pid: name(pid, '(no one)'))
        for r in rows:
            since = committer_since.get(r['person'])
            r['committer'] = since is not None and since <= year
        years[year]['commits'] = {
            'count': n,
            'first': first.committed_at.isoformat(sep=' '),
            'last': last.committed_at.isoformat(sep=' '),
            'complete': year < latest.year,
            'authors': rows,
            'for_others': ranked(for_others[year], ['lines', 'commits'],
                                 lambda pid: name(pid, None)),
        }

    mail = {}
    for directory in sorted(glob.glob(os.path.join(root, 'data', 'mail', '*'))):
        mailing_list = os.path.basename(directory)
        months_path = os.path.join(directory, 'months.tsv')
        if not os.path.exists(months_path):
            sys.exit(f'{months_path} is missing; rerun extract-mail.py')
        months = [m for _, (m,) in commitdata.read_tsv(months_path, MONTH_COLUMNS, root)]
        mail[mailing_list] = {'months': months}
        for path in sorted(glob.glob(os.path.join(directory, '[0-9]' * 4 + '.tsv'))):
            year = int(os.path.basename(path)[:4])
            senders = collections.defaultdict(lambda: {'messages': 0})
            n = 0
            for _, (_, _, from_name, address) in commitdata.read_tsv(
                    path, people.MAIL_COLUMNS, root):
                pid, status = p.resolve(people.clean_name(from_name), address)
                # As in top_senders, messages from what isn't a person, such
                # as the web forms, aren't counted.
                if status != 'nobody':
                    senders[pid]['messages'] += 1
                    n += 1
            loaded = [m for m in months if m.startswith(f'{year}-')]
            years[year].setdefault('mail', {})[mailing_list] = {
                'count': n,
                'months': loaded,
                'complete': len(loaded) == 12,
                'senders': ranked(senders, ['messages'],
                                  lambda pid: name(pid, '(unidentified)')),
            }

    result = {
        'generated': datetime.datetime.now(datetime.timezone.utc)
                     .isoformat(sep=' ', timespec='seconds'),
        'latest_commit': {'hash': latest.hash,
                          'committed_at': latest.committed_at.isoformat(sep=' ')},
        'mail': mail,
        'years': {str(y): years[y] for y in sorted(years)},
    }
    out = open(args.output, 'w', encoding='utf-8') if args.output else sys.stdout
    json.dump(result, out, ensure_ascii=False, indent=1)
    out.write('\n')
    if args.output:
        out.close()


if __name__ == '__main__':
    main()
