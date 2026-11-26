"""
Load commit data and its overrides into memory.

    import commitdata
    data = commitdata.load()
    for commit in data.commits.values():
        print(commit.hash, commit.tags)

Reads data/commits/*.tsv and data/tags/*.tsv, as written by
extract-commits.py, and applies overrides/tag-fixes/*.tsv.  Also reads
data/line-adjustments/*.tsv, as written by classify-commits.py, and
overrides/line-adjustments/*.tsv, and from them works out each commit's
credited lines.  Overrides are applied permissively, so a stale or
contradictory one doesn't stop anything that uses this module;
check-commits.py reports those.  A file that can't be read at all raises
LoadError.
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

LINE_ADJUSTMENT_COLUMNS = ['hash', 'kind', 'details']
LINE_ADJUSTMENT_KINDS = ('revert', 'mechanical', 'normal')

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
    adjustment: object = None       # the LineAdjustment in effect, if any
    reverted_by: str = None         # the revert in effect, if any
    reverted_at: datetime.date = None   # its date, UTC
    credited_lines: int = 0         # insertions that count as effort
    is_revert: bool = False         # it reverts other commits
    utc_date: datetime.date = None  # committed_at's date in UTC


@dataclasses.dataclass
class TagFix:
    hash: str
    verb: str
    tag: str
    value: str
    year: int                       # the year of the file it came from
    where: str                      # "file:line", for messages


@dataclasses.dataclass
class LineAdjustment:
    hash: str
    kind: str
    details: dict                   # e.g. {'reverted-by': ..., 'residual': '40'}
    year: int                       # the year of the file it came from
    where: str                      # "file:line", for messages
    override: bool                  # from overrides/ rather than data/

    @property
    def residual(self):
        """Lines still credited; 0 if absent or unreadable."""
        try:
            return max(0, int(self.details.get('residual', 0)))
        except ValueError:
            return 0


@dataclasses.dataclass
class CommitData:
    commits: dict                   # hash -> Commit, oldest first
    tag_fixes: list                 # [TagFix] in file order
    line_adjustments: list          # [LineAdjustment], data then overrides

    def credited_lines(self, commit, reverts_until=None):
        """
        The commit's credited lines, counting only reverts made on or
        before reverts_until (a date), if given.  With
        reverts_until=datetime.date(commit.year, 12, 31), a commit loses
        its credit only to a revert in the same year.
        """
        return credit(self.commits, commit, reverts_until)


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
            commits[h].utc_date = committed_at.astimezone(datetime.timezone.utc).date()

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

    line_adjustments = []
    for sub, override in (('data', False), ('overrides', True)):
        for path in sorted(glob.glob(os.path.join(root, sub, 'line-adjustments',
                                                  '*.tsv'))):
            year = int(os.path.basename(path)[:-4])
            for where, (h, kind, details) in read_tsv(
                    path, LINE_ADJUSTMENT_COLUMNS, root, comments=override,
                    optional=1 if override else 0):
                line_adjustments.append(LineAdjustment(
                    h, kind, parse_details(details), year, where, override))
    apply_line_adjustments(commits, line_adjustments)

    return CommitData(commits, tag_fixes, line_adjustments)


def parse_details(details):
    """'a=1,b=2' -> {'a': '1', 'b': '2'}; pieces without '=' are skipped."""
    out = {}
    for piece in details.split(','):
        key, eq, value = piece.strip().partition('=')
        if eq:
            out[key.strip()] = value.strip()
    return out


def utc_date(committed_at):
    """'2026-11-19 13:25:46 +0200' -> datetime.date in UTC"""
    t = datetime.datetime.strptime(committed_at, '%Y-%m-%d %H:%M:%S %z')
    return t.astimezone(datetime.timezone.utc).date()


def apply_line_adjustments(commits, adjustments):
    """
    Set each commit's adjustment, reverted_by and credited_lines.

    An override row replaces the data rows for its commit; "normal" cancels
    them.  A mechanical commit is credited its residual, or nothing.  So is
    a reverted one, unless the revert was itself reverted.  A commit that
    reverts others is credited nothing, unless it has a row of its own.
    Rows for unknown commits or of unknown kinds are ignored.
    """
    for commit in commits.values():
        commit.adjustment = None
    for a in adjustments:
        if a.hash in commits and a.kind in LINE_ADJUSTMENT_KINDS:
            commit = commits[a.hash]
            if commit.adjustment is None or (a.override and not commit.adjustment.override):
                commit.adjustment = a
    reverting = {c.adjustment.details.get('reverted-by') for c in commits.values()
                 if c.adjustment and c.adjustment.kind == 'revert'}
    for commit in commits.values():
        commit.is_revert = commit.hash in reverting
    for commit in commits.values():
        commit.reverted_by = reverted(commits, commit.hash)
        commit.reverted_at = commits[commit.reverted_by].utc_date if commit.reverted_by else None
        commit.credited_lines = credit(commits, commit)


def reverted(commits, h, until=None, depth=0):
    """The revert in effect for h (made by `until`, if given), or None."""
    a = commits[h].adjustment
    if depth > 50 or a is None or a.kind != 'revert':
        return None
    by = a.details.get('reverted-by')
    if by not in commits or (until and commits[by].utc_date > until):
        return None
    if reverted(commits, by, until, depth + 1):
        return None                 # a reverted revert doesn't count
    return by


def credit(commits, commit, until=None):
    """Credited lines for commit, counting only reverts made by `until`."""
    a = commit.adjustment
    if a is None:
        return 0 if commit.is_revert else commit.insertions
    if a.kind == 'normal':
        return commit.insertions
    if a.kind == 'mechanical' or reverted(commits, commit.hash, until):
        return min(a.residual, commit.insertions)
    return 0 if commit.is_revert else commit.insertions


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


def read_tsv(path, columns, root, comments=False, optional=0):
    """
    Yield ("file:line", fields) for each row of a COPY text format file,
    after checking its header.  The file name is given relative to root.
    With comments, blank lines and lines starting with # are skipped.  The
    last `optional` columns may be left off a row; they come back empty.
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
            elif not len(columns) - optional <= len(fields) <= len(columns):
                raise LoadError(f'{where}: expected {len(columns)} fields, '
                                f'found {len(fields)}')
            else:
                yield where, fields + [''] * (len(columns) - len(fields))


def unescape(value):
    return re.sub(r'\\(.)',
                  lambda m: {'t': '\t', 'n': '\n', 'r': '\r'}.get(m[1], m[1]),
                  value)
