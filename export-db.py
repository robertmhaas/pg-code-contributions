#!/usr/bin/env python3
"""
Write a PostgreSQL dump of the contribution data, for running reports.

    python3 export-db.py -o contributions.sql
    createdb contributions
    psql -q -f contributions.sql contributions

The dump drops and recreates its own tables and views, so it can be
loaded again over an earlier one.  Tables, each described in SCHEMA:

- people: a stanza of people.txt each, with its Canonical: value as id.
- commits: the columns of data/commits/, plus the committer and primary
  author as people, credited lines with all reverts and with only
  same-year reverts, and the line adjustment.
- commit_tags: a tag each, after tag fixes, with its canonical role and
  person for person tags.
- emails: a mailing list message each, with the sender as a person.

The committer of a commit is its git author, which PostgreSQL's committers
set to themselves; a commit pushed by someone else, such as a security
release, keeps the committer of record.

Views, in INDEXES_AND_VIEWS, for the usual reports: committers,
top_authors (as in the yearly posts), top_opp_committers, credits and
top_senders.

    SELECT "#", author, lines, pct_lines, cum_pct_lines, commits
      FROM top_authors WHERE year = 2025 ORDER BY "#" LIMIT 30;
"""

import argparse
import datetime
import glob
import os
import sys

import commitdata
import people

SCHEMA = '''\
DROP VIEW IF EXISTS top_authors, top_opp_committers, top_senders, credits, committers CASCADE;
DROP TABLE IF EXISTS emails, commit_tags, commits, people CASCADE;

CREATE TABLE people (
    id text PRIMARY KEY,            -- its Canonical: line in people.txt
    name text NOT NULL,             -- the name to show
    address text,                   -- its canonical address
    kind text NOT NULL              -- 'person', or 'none' for tools and the like
);

CREATE TABLE commits (
    hash text PRIMARY KEY,
    position integer NOT NULL,      -- order on master, from 1
    year integer NOT NULL,
    committed_at timestamptz NOT NULL,
    subject text NOT NULL,
    git_author text NOT NULL,       -- %an, the committer of record
    committer text REFERENCES people,   -- the git author, as a person
    insertions integer NOT NULL,
    credited_lines integer NOT NULL,          -- after line adjustments
    credited_lines_in_year integer NOT NULL,  -- ... counting only reverts in the same year
    adjustment text,                -- 'revert' or 'mechanical'
    reverted_by text,
    reverted_at date,
    is_revert boolean NOT NULL,
    primary_author text REFERENCES people,    -- credited with the lines
    primary_author_value text       -- as written in the tag, or the git author
);

CREATE TABLE commit_tags (
    hash text NOT NULL REFERENCES commits,
    position integer NOT NULL,      -- order in the message, after tag fixes
    tag text NOT NULL,              -- as written
    key text NOT NULL,              -- lower case, spaces as hyphens
    role text,                      -- the canonical credit key, for person tags
    value text NOT NULL,
    person text REFERENCES people,
    status text,                    -- for person tags: 'ok', 'nobody', 'list', ...
    PRIMARY KEY (hash, position)
);

CREATE TABLE emails (
    list text NOT NULL,
    message_id text NOT NULL,
    year integer NOT NULL,          -- the year of the archive's file
    sent_at timestamptz NOT NULL,
    from_name text NOT NULL,
    from_address text,
    person text REFERENCES people,
    status text NOT NULL,           -- 'ok', 'nobody', ...
    PRIMARY KEY (list, message_id)
);
'''

INDEXES_AND_VIEWS = '''\
CREATE INDEX ON commits (year);
CREATE INDEX ON commits (primary_author);
CREATE INDEX ON commit_tags (person);
CREATE INDEX ON emails (list, year);
CREATE INDEX ON emails (person);

-- Committers: people who are the git author of a commit, since their first.
CREATE VIEW committers AS
SELECT committer AS person, min(committed_at) AS since, count(*) AS commits
  FROM commits
 WHERE committer IS NOT NULL
 GROUP BY committer;

-- Credited lines by primary author, as in the yearly posts: reverts in a
-- later year don't take lines away.  [*] marks someone who wasn't yet a
-- committer at the end of the year.
CREATE VIEW top_authors AS
SELECT x.year,
       row_number() OVER w AS "#",
       coalesce(p.name, '(no one)') ||
           CASE WHEN c.since < make_date(x.year + 1, 1, 1) THEN '' ELSE ' [*]' END AS author,
       x.author AS person,
       x.lines,
       (x.lines * 100.0 / sum(x.lines) OVER y)::numeric(10,2) AS pct_lines,
       (sum(x.lines) OVER (w ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) * 100.0
            / sum(x.lines) OVER y)::numeric(10,2) AS cum_pct_lines,
       x.commits
  FROM (SELECT year, primary_author AS author,
               sum(credited_lines_in_year) AS lines, count(*) AS commits
          FROM commits GROUP BY year, primary_author) x
  LEFT JOIN people p ON p.id = x.author
  LEFT JOIN committers c ON c.person = x.author
WINDOW y AS (PARTITION BY x.year),
       w AS (PARTITION BY x.year ORDER BY x.lines DESC, x.commits DESC, p.name);

-- Credited lines of other people's commits, by the committer who committed them.
CREATE VIEW top_opp_committers AS
SELECT year,
       row_number() OVER w AS "#",
       p.name AS committer,
       x.committer AS person,
       lines,
       (lines * 100.0 / sum(lines) OVER y)::numeric(10,2) AS pct_lines,
       (sum(lines) OVER (w ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) * 100.0
            / sum(lines) OVER y)::numeric(10,2) AS cum_pct_lines,
       commits
  FROM (SELECT year, committer, sum(credited_lines_in_year) AS lines, count(*) AS commits
          FROM commits
         WHERE committer <> primary_author
         GROUP BY year, committer) x
  JOIN people p ON p.id = x.committer
WINDOW y AS (PARTITION BY year), w AS (PARTITION BY year ORDER BY lines DESC, commits DESC, p.name);

-- Credits by role (Author, Reviewed-by, ...), person and year.
CREATE VIEW credits AS
SELECT c.year, t.role, t.person, p.name, count(*) AS credits
  FROM commit_tags t
  JOIN commits c USING (hash)
  JOIN people p ON p.id = t.person
 WHERE t.role IS NOT NULL
 GROUP BY c.year, t.role, t.person, p.name;

-- Messages by sender, list and year.
CREATE VIEW top_senders AS
SELECT list, year,
       row_number() OVER (PARTITION BY list, year ORDER BY count(*) DESC, p.name) AS "#",
       coalesce(p.name, '(unidentified)') AS sender,
       e.person,
       count(*) AS messages
  FROM emails e
  LEFT JOIN people p ON p.id = e.person
 WHERE e.status <> 'nobody'
 GROUP BY list, year, e.person, p.name;
'''


def escape(value):
    if value is None:
        return '\\N'
    return (str(value).replace('\\', '\\\\').replace('\t', '\\t')
            .replace('\n', '\\n').replace('\r', '\\r'))


def copy(out, table, columns, rows):
    out.write(f'COPY {table} ({", ".join(columns)}) FROM stdin;\n')
    n = 0
    for row in rows:
        out.write('\t'.join(escape(v) for v in row) + '\n')
        n += 1
    out.write('\\.\n\n')
    return n


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

    def person_rows():
        for pid, person in p.persons.items():
            yield pid, person.name, person.address, person.kind

    def commit_rows():
        for position, c in enumerate(data.commits.values(), 1):
            committer, _ = p.resolve(people.clean_name(c.git_author))
            primary = p.primary_author(c)
            year_end = datetime.date(c.year, 12, 31)
            yield (c.hash, position, c.year, c.committed_at.isoformat(sep=' '), c.subject,
                   c.git_author, committer, c.insertions, c.credited_lines,
                   data.credited_lines(c, reverts_until=year_end),
                   c.adjustment.kind if c.adjustment else None,
                   c.reverted_by, c.reverted_at, 't' if c.is_revert else 'f',
                   primary[1] if primary else None, primary[0] if primary else None)

    roles = {t.lower(): t for t in people.PERSON_TAGS}

    def tag_rows():
        for c in data.commits.values():
            for position, (tag, value) in enumerate(c.tags, 1):
                key = people.tag_key(tag)
                if people.is_person_tag(tag):
                    pid, status = p.resolve_value(value)
                    yield c.hash, position, tag, key, roles[key], value, pid, status
                else:
                    yield c.hash, position, tag, key, None, value, None, None

    def email_rows():
        for path in sorted(glob.glob(os.path.join(root, 'data', 'mail', '*', '*.tsv'))):
            mailing_list = os.path.basename(os.path.dirname(path))
            year = int(os.path.basename(path)[:4])
            for _, (message_id, sent_at, name, address) in commitdata.read_tsv(
                    path, people.MAIL_COLUMNS, root):
                pid, status = p.resolve(people.clean_name(name), address)
                yield (mailing_list, message_id, year, sent_at, name,
                       people.address_key(address) if address else None, pid, status)

    out = open(args.output, 'w', encoding='utf-8') if args.output else sys.stdout
    out.write('-- Generated by export-db.py from pg-code-contributions; load with psql -f.\n'
              'SET client_encoding = \'UTF8\';\nSET client_min_messages = warning;\nBEGIN;\n\n' + SCHEMA + '\n')
    counts = {
        'people': copy(out, 'people', ['id', 'name', 'address', 'kind'], person_rows()),
        'commits': copy(out, 'commits', [
            'hash', 'position', 'year', 'committed_at', 'subject', 'git_author', 'committer',
            'insertions', 'credited_lines', 'credited_lines_in_year', 'adjustment',
            'reverted_by', 'reverted_at', 'is_revert', 'primary_author',
            'primary_author_value'], commit_rows()),
        'commit_tags': copy(out, 'commit_tags', [
            'hash', 'position', 'tag', 'key', 'role', 'value', 'person', 'status'], tag_rows()),
        'emails': copy(out, 'emails', [
            'list', 'message_id', 'year', 'sent_at', 'from_name', 'from_address', 'person',
            'status'], email_rows()),
    }
    out.write(INDEXES_AND_VIEWS + '\nCOMMIT;\n')
    if args.output:
        out.close()
    print(', '.join(f'{n} {t}' for t, n in counts.items()), file=sys.stderr)


if __name__ == '__main__':
    main()
