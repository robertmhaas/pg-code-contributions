"""
Load commit data and its overrides into memory.

    import commitdata
    data = commitdata.load()
    for commit in data.commits.values():
        print(commit.hash, commit.tags)

Reads data/commits/*.tsv and data/tags/*.tsv, as written by
extract-commits.py, and applies overrides/tag-fixes/*.tsv.  Fixes are applied
permissively, so a stale or contradictory fix doesn't stop anything that
uses this module; check-commits.py reports those.  A file that can't be read
at all raises LoadError.
"""

import dataclasses
import datetime
import glob
import os
import re

COMMIT_COLUMNS = ['hash', 'committer', 'committed_at', 'insertions', 'subject']
TAG_COLUMNS = ['hash', 'tag', 'value']
TAG_FIX_COLUMNS = ['hash', 'verb', 'tag', 'value']

TAG_FIX_VERBS = ('insert', 'append', 'delete', 'replace')

ROOT = os.path.dirname(os.path.abspath(__file__))


@dataclasses.dataclass
class Commit:
    hash: str
    year: int                       # the year of the file it came from
    committer: str
    committed_at: datetime.datetime
    insertions: int
    subject: str
    raw_tags: list                  # [(tag, value)] as extracted
    tags: list                      # [(tag, value)] with fixes applied


@dataclasses.dataclass
class TagFix:
    hash: str
    verb: str
    tag: str
    value: str
    year: int                       # the year of the file it came from
    where: str                      # "file:line", for messages


@dataclasses.dataclass
class CommitData:
    commits: dict                   # hash -> Commit, oldest first
    tag_fixes: list                 # [TagFix] in file order


class LoadError(Exception):
    pass


def load(root=ROOT):
    commits = {}
    for path in sorted(glob.glob(os.path.join(root, 'data', 'commits', '*.tsv'))):
        year = int(os.path.basename(path)[:-4])
        for where, (h, committer, committed_at, insertions, subject) \
                in read_tsv(path, COMMIT_COLUMNS, root):
            if h in commits:
                raise LoadError(f'{where}: duplicate commit {h}')
            try:
                committed_at = datetime.datetime.strptime(
                    committed_at, '%Y-%m-%d %H:%M:%S %z')
                insertions = int(insertions)
            except ValueError as e:
                raise LoadError(f'{where}: {e}') from None
            commits[h] = Commit(h, year, committer, committed_at, insertions,
                                subject, [], [])

    for path in sorted(glob.glob(os.path.join(root, 'data', 'tags', '*.tsv'))):
        for where, (h, tag, value) in read_tsv(path, TAG_COLUMNS, root):
            if h not in commits:
                raise LoadError(f'{where}: tag for unknown commit {h}')
            commits[h].raw_tags.append((tag, value))

    tag_fixes = []
    for path in sorted(glob.glob(os.path.join(root, 'overrides', 'tag-fixes',
                                              '*.tsv'))):
        year = int(os.path.basename(path)[:-4])
        tag_fixes += [TagFix(*fields, year, where)
                      for where, fields in read_tsv(path, TAG_FIX_COLUMNS, root,
                                                    comments=True)]
    fixes_by_hash = {}
    for fix in tag_fixes:
        fixes_by_hash.setdefault(fix.hash, []).append(fix)
    for commit in commits.values():
        commit.tags = apply_tag_fixes(commit.raw_tags,
                                      fixes_by_hash.get(commit.hash, []))

    return CommitData(commits, tag_fixes)


def apply_tag_fixes(raw_tags, fixes):
    """
    Return raw_tags with fixes applied, each tag separately.

    For a tag with a replace, the replace values, in file order, take the
    place of all its existing values, at the position of the first one.
    Otherwise inserts go just before the tag's first existing value, appends
    just after its last, and deletes remove every value that matches
    exactly.  If the commit has no values for the tag, inserts go before all
    its tags and appends after them.

    Anything check-commits.py would complain about is applied anyway, as far
    as that makes sense: a replace overrides other verbs for the same tag,
    unknown verbs and deletes that match nothing are ignored, and inserting
    a value that's already present adds a second copy.
    """
    tags = list(raw_tags)
    fixes_by_tag = {}
    for fix in fixes:
        fixes_by_tag.setdefault(fix.tag, []).append(fix)

    for tag, tag_fixes in fixes_by_tag.items():
        def values(verb):
            return [(tag, f.value) for f in tag_fixes if f.verb == verb]
        positions = [i for i, (t, _) in enumerate(tags) if t == tag]

        if any(f.verb == 'replace' for f in tag_fixes):
            start = positions[0] if positions else len(tags)
            others = [entry for entry in tags if entry[0] != tag]
            tags = others[:start] + values('replace') + others[start:]
        else:
            start = positions[0] if positions else 0
            end = positions[-1] + 1 if positions else len(tags)
            deleted = {v for _, v in values('delete')}
            middle = [entry for entry in tags[start:end]
                      if not (entry[0] == tag and entry[1] in deleted)]
            tags = (tags[:start] + values('insert') + middle
                    + values('append') + tags[end:])
    return tags


def read_tsv(path, columns, root, comments=False):
    """
    Yield ("file:line", fields) for each row of a COPY text format file,
    after checking its header.  The file name is given relative to root.
    With comments, blank lines and lines starting with # are skipped.
    """
    header_seen = False
    with open(path, encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            line = line.rstrip('\n')
            if comments and (line.startswith('#') or not line.strip()):
                continue
            where = f'{os.path.relpath(path, root)}:{lineno}'
            fields = [unescape(v) for v in line.split('\t')]
            if not header_seen:
                if fields != columns:
                    raise LoadError(f'{where}: expected header '
                                    f'{" ".join(columns)!r}')
                header_seen = True
            elif len(fields) != len(columns):
                raise LoadError(f'{where}: expected {len(columns)} fields, '
                                f'found {len(fields)}')
            else:
                yield where, fields


def unescape(value):
    return re.sub(r'\\(.)',
                  lambda m: {'t': '\t', 'n': '\n', 'r': '\r'}.get(m[1], m[1]),
                  value)
