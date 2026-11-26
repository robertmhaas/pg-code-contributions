"""Split "Authors: A, B" and "Reviewers: C, D" into one canonical tag per person."""
import glob, os, re, sys, unicodedata
repo = sys.argv[1]
sys.path.insert(0, repo); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import commitdata, roles
KEYS = {
    'Authors': 'Author', 'Submitted by': 'Author', 'submitted by': 'Author', 'Patch by': 'Author',
    'Patch-by': 'Author', 'Patch-By': 'Author', 'Patch': 'Author', 'patch': 'Author',
    'Co-Authored-by': 'Co-authored-by', 'Co-Authored-By': 'Co-authored-by', 'Co-author': 'Co-authored-by',
    'Based-on-patch-by': 'Co-authored-by',
    'Reviewers': 'Reviewed-by', 'Reviewer': 'Reviewed-by', 'Review': 'Reviewed-by', 'Review by': 'Reviewed-by',
    'Reviewed by': 'Reviewed-by', 'Reviewed By': 'Reviewed-by', 'Reviewed-By': 'Reviewed-by',
    'Reviewd by': 'Reviewed-by', 'Reviewd-By': 'Reviewed-by', 'Reviwed-by': 'Reviewed-by',
    'Reported-By': 'Reported-by', 'Reported by': 'Reported-by', 'Report by': 'Reported-by',
    'Tested-By': 'Tested-by',
    'Suggested by': 'Suggested-by', 'Suggested-By': 'Suggested-by', 'Fix-suggested-by': 'Suggested-by',
    'Diagnosed-By': 'Diagnosed-by', 'Diagnosys-by': 'Diagnosed-by', 'Diagnozed-by': 'Diagnosed-by',
    'Analyzed-by': 'Diagnosed-by', 'Analyzed-By': 'Diagnosed-by',
}
PREFIX = {'Author': 'Patch by', 'Co-authored-by': 'Additional work by', 'Reviewed-by': 'Reviewed by',
          'Reported-by': 'Reported by', 'Tested-by': 'Tested by', 'Suggested-by': 'Suggested by',
          'Diagnosed-by': 'Diagnosed by'}
GLOSS = {'Based-on-patch-by': 'earlier patch', 'Fix-suggested-by': 'fix'}
HAND = {
    ('9bffaade96f', 'Submitted by'): [('Author', 'Chris Dunlop <chris@onthe.net.au>', None)],
    ('e66197fa2ef', 'Reported-By'): [('Reported-by', 'Andres Freund (valgrind / buildfarm animal skink)', None)],
    ('7a1d4a2448c', 'Reported-By'): [('Reported-by', 'Andres Freund (valgrind / buildfarm animal skink)', None)],
    ('bfa2ab56bb8', 'Reported-By'): [('Reported-by', 'Thomas Munro', 'different aspects'), ('Reported-by', 'Noah Misch', 'different aspects')],
    ('8e12f4a250d', 'Reviewed by'): [('Reviewed-by', 'Robert Haas', None)],
    ('98a64d0bd71', 'Reported-By'): [('Reported-by', 'Dmitry Vasilyev', None),
                    ('Discussion', 'CAB-SwXZh44_2ybvS5Z67p_CDz=XFn4hNAD=CnMEF+QqkXwFrGg@mail.gmail.com', None),
                    ('Discussion', '20160114143931.GG10941@awork2.anarazel.de', None)],
    ('b488c580aef', 'Authors'): [('Author', 'Álvaro Herrera', None)]
                   + [('Co-authored-by', n, None) for n in ('Andres Freund', 'Ian Barwick', 'Abhijit Menon-Sen')]
                   + [('Reviewed-by', n, None) for n in ('Andres Freund', 'Robert Haas', 'Amit Kapila', 'Michael Paquier', 'Craig Ringer', 'David Steele')]
                   + [('Suggested-by', 'Dimitri Fontaine (original work)', None)]
                   + [('Suggested-by', n, None) for n in ('Chris Browne', 'Stephen Frost', 'Petr Jelínek', 'Tom Lane', 'Jim Nasby', 'Steven Singer', 'Pavel Stěhule')],
    ('bd7c95f0c1a', 'Authors'): [('Author', '"Ideriha, Takeshi" <ideriha.takeshi@jp.fujitsu.com>', None),
                    ('Author', '"Kuroda, Hayato" <kuroda.hayato@jp.fujitsu.com>', None)],
}
def bare(v): return roles.fold(roles.bare(v)).replace('"', '').strip()
def normkey(t): return re.sub(r'[\s_]+', '-', t.strip().lower())

def read_blocks(path):
    lines = open(path, encoding='utf-8').read().rstrip('\n').split('\n')
    i = lines.index('hash\tverb\ttag\tvalue') + 1
    blocks, cur = [], None
    for l in lines[i:]:
        if not l.strip(): cur = None; continue
        if cur is None or (l.startswith('#') and cur[1]): cur = ([], []); blocks.append(cur)
        (cur[0] if l.startswith('#') else cur[1]).append(l)
    return lines[:i], blocks

def strip_previous_splits():
    for path in glob.glob(os.path.join(repo, 'overrides', 'tag-fixes', '*.tsv')):
        header, blocks = read_blocks(path)
        kept, changed = [], False
        for comments, rows in blocks:
            comments0 = comments
            if any(c.endswith('one tag per person.') or c.endswith('continues on the next line.') for c in comments):
                changed = True
                comments = [c for c in comments if not (c.endswith('one tag per person.') or c.endswith('continues on the next line.'))]
                cut = next((i for i, r in enumerate(rows) if (r.split('\t')[1] == 'delete' and r.split('\t')[2] in KEYS)
                            or (r.split('\t')[1] == 'replace' and r == rows[-1] and any(c.endswith('continues on the next line.') for c in comments0))), len(rows))
                rows = rows[:cut]
            if rows:
                kept.append((comments, rows))
        if changed:
            out = list(header)
            for comments, rows in kept:
                out += [''] + comments + rows
            open(path, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
strip_previous_splits()
data = commitdata.load(repo)
order = {h: i for i, h in enumerate(data.commits)}

import subprocess
PGSQL = sys.argv[2]
CREDIT_KEYS = {'author', 'authors', 'co-authored-by', 'reviewed-by', 'reviewed by', 'reviewers',
               'reported-by', 'reported by', 'tested-by', 'suggested-by', 'diagnosed-by',
               'submitted by', 'review', 'reviewer'}
TAG_LINE = re.compile(r'^[A-Za-z][\w -]*:\s')

def completed(h, t, v):
    """The full value of a tag whose text runs on to unindented lines."""
    if not re.search(r'(,|\band|&)\s*$', v):
        return v
    lines = subprocess.run(['git', '-C', PGSQL, 'log', '-1', '--format=%B', h],
                           capture_output=True, text=True).stdout.split('\n')
    for i, l in enumerate(lines):
        if re.match(rf'{re.escape(t)}\s*:', l):
            full = v
            for nxt in lines[i + 1:]:
                if not re.search(r'(,|\band|&)\s*$', full) or not nxt.strip() or TAG_LINE.match(nxt):
                    break
                full = f'{full} {nxt.strip()}'
            return full
    return v

new, unparsed, continued, skipped = {}, [], [], []
have_by_commit, authors_by_commit = {}, {}
fixed_tags = {(f.hash, f.tag) for f in data.tag_fixes if 'one tag per person' not in f.where}
MIN_YEAR = 2011     # older commits aren't being cleaned up (yet)
for c in data.commits.values():
    if c.year < MIN_YEAR:
        continue
    for t, v in c.raw_tags:
        if t.lower() not in CREDIT_KEYS and t not in KEYS:
            continue
        full = completed(c.hash, t, v) if (c.hash, t) not in HAND else v
        if t not in KEYS:
            if full != v:
                n_same = sum(1 for tt, _ in c.raw_tags if tt == t)
                if (c.hash, t) in fixed_tags or n_same > 1:
                    skipped.append((c.hash, t, v)); continue
                new.setdefault(c.hash, []).append(
                    (f'# "{t}: {v}" continues on the next line.', [f'{c.hash}\treplace\t{t}\t{full}']))
                continued.append(c.hash)
            continue
        canon = KEYS[t]
        if full != v:
            continued.append(c.hash)
        if (c.hash, t) in HAND:
            tagged = HAND[c.hash, t]
        else:
            if t.lower() == 'patch' and (re.fullmatch(r'<[^>]+@[^>]+>', full.strip()) or not roles.people(full, c.git_author, strict=False)):
                continue    # "Patch: <message-id>" or stray prose, not a person
            names = roles.people(full, c.git_author, strict=False)
            if names and len(names) == 1 and normkey(t) == normkey(canon) and full == v:
                continue    # one name, key differs only in case/spacing: the next layer normalizes that
            if names:
                tagged = [(canon, n, GLOSS.get(t)) for n in names]
            else:
                # prose in the value: "A and B.  Some tweaks by C"
                ptags, confident, _, _ = roles.parse(full if canon == 'Author' else f'{PREFIX[canon]} {full}', c.git_author)
                plain = re.sub(r'"[^"]*"|\([^)]*\)|<[^>]*>', '', full)
                if confident:
                    tagged = ptags
                elif not re.search(r',|\band\b|&|/|\b(by|with|from)\b', plain):
                    # one person, written some other way ("bruce", "x@y (Name)"): keep it as written
                    if normkey(t) == normkey(canon) and full == v:
                        continue
                    tagged = [(canon, full.strip().rstrip('.'), GLOSS.get(t))]
                else:
                    unparsed.append((c.hash, t, v)); continue
        have = have_by_commit.setdefault(c.hash, {(normkey(tt), bare(x)) for tt, x in c.tags})
        authors = authors_by_commit.setdefault(c.hash, {bare(x) for tt, x in c.tags if tt == 'Author'})
        has_authors = bool(authors)
        rows = [f'{c.hash}\tdelete\t{t}\t{v}']
        for tt, n, g in tagged:
            val = f'{n} ({g})' if g and '(' not in n else n
            if (normkey(tt), bare(n)) in have or (tt == 'Co-authored-by' and bare(n) in authors): continue
            have.add((normkey(tt), bare(n)))
            if tt == 'Author': authors.add(bare(n))
            verb = 'insert' if tt == 'Author' and not has_authors else 'append'
            rows.append(f'{c.hash}\t{verb}\t{tt}\t{val}')
        new.setdefault(c.hash, []).append((f'# "{t}: {v}", one tag per person.', rows))

HEADER = lambda y: [f'# Tag fixes for {y} commits: tags that extract-commits.py misreads or',
                    '# misses, and authors credited only in prose.  Rows are in commit order,',
                    '# each preceded by a comment.  See README.md for what each verb does.',
                    'hash\tverb\ttag\tvalue']
files = {}
for path in glob.glob(os.path.join(repo, 'overrides', 'tag-fixes', '*.tsv')):
    header, blocks = read_blocks(path)
    # drop split blocks from an earlier run so this is repeatable
    files[int(os.path.basename(path)[:-4])] = (header, blocks)
added = 0
for h, items in new.items():
    y = data.commits[h].year
    header, blocks = files.setdefault(y, (HEADER(y), []))
    existing = next((blk for blk in blocks if blk[1] and blk[1][0].split('\t')[0] == h), None)
    for comment, rows in items:
        if existing:
            existing[0].append(comment); existing[1].extend(rows)
        else:
            existing = ([comment], list(rows)); blocks.append(existing)
        added += len(rows)
for y, (header, blocks) in files.items():
    keyed = sorted(enumerate(blocks), key=lambda ib: (order[ib[1][1][0].split('\t')[0]], ib[0]))
    out = list(header)
    for _, (comments, rows) in keyed:
        out += [''] + comments + rows
    with open(os.path.join(repo, 'overrides', 'tag-fixes', f'{y}.tsv'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')
print(len(set(continued)), 'values completed from continuation lines; skipped (already fixed elsewhere):', skipped)
print(len(new), 'commits split,', added, 'rows; not parsed:')
for u in unparsed: print('  ', u)
