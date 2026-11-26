"""
Parse prose attribution ("Report and patch by X, reviewed by Y") into tags.

usage: python3 -B roles.py REPO PGSQL out.json
For every "insert Author" fix that came from prose, proposes the full set of
tags for that commit and says whether the parse is confident (and if not,
why).  A parse is confident only if every clause that credits someone was
understood and every person the sentences name got a tag.
"""
import json
import re
import subprocess
import sys
import unicodedata


UPPER = 'A-ZÀ-ÞĀ-ſ'


def fold(s):
    s = unicodedata.normalize('NFKD', s)
    return ''.join(c for c in s if not unicodedata.combining(c)).lower()


def bare(v):
    return re.sub(r'\s*(<[^>]*>|\([^)]*\))', '', v).strip()


ROLE_WORDS = [
    ('Reported-by', r'report|gripe|complain|bug\s*#|noticed|\bnoted\b|spotted|pointed out|\bfound\b|discovered|raised|^per$|^per\b(?!.*(suggest|idea))'),
    ('Diagnosed-by', r'diagnos|analy[sz]|investigat|tracked down|isolat|identified'),
    ('Author', r'\bpatch|\bfix\b|fixed|author|written|wrote|\bcode\b|coded|implement|submitted|initial version|extracted|\bcore\b'),
    ('Reviewed-by', r'review|nitpick|bikeshed|feedback|critique'),
    ('Tested-by', r'\btest(ed|ing)\b|confirmed|verified'),
    ('Suggested-by', r'suggest|idea|propos(al|ed)\b(?!.*(patch|fix))|design|sketch'),
]
EDITING = re.compile(
    r'edit|tweak|adjust|revis|rework|cleanup|clean-up|cosmetic|comment|doc|'
    r'change|modif|improv|polish|hack|help|contribut|addition|added|rewr|'
    r'refactor|wordsmith|word-smith|massag|whack|mangl|extend|expand|\bwork\b|'
    r'commentary|further|more|test\s*case|\btests?\b|regression test|fixes|'
    r'correct|subtract|remov|trim|pgindent|\bmods?\b|kibitz|kabitz', re.I)
EARLIER = re.compile(r'based on|earlier|previous|original|initial|prior|originally', re.I)
CONNECTIVE = re.compile(r'^(?:but|and|also|as well|,|\s)*$', re.I)
COMMIT_REF = re.compile(r'^\s*(?:commit\s+)?[0-9a-f]{7,40}\s*$', re.I)

NOT_PEOPLE = re.compile(
    r'^(?:coverity|sqlsmith|valgrind|buildfarm|irc|others|many others|several others|'
    r'various others|various people|lots of people|many|users|the buildfarm|'
    r'.*\bbuildfarm\b.*)$', re.I)
NOT_NAMES = {
    'documentation', 'discussion', 'report', 'reports', 'review', 'reviewed',
    'patch', 'patches', 'bug', 'author', 'authors', 'test', 'tests', 'testing',
    'code', 'comments', 'original', 'initial', 'problem', 'analysis',
    'diagnosis', 'idea', 'back-patch', 'backpatch', 'also', 'per', 'the',
    'this', 'that', 'it', 'we', 'some', 'see', 'thanks', 'fix', 'further',
    'additional', 'minor', 'several', 'various', 'all', 'other', 'committer',
    'reporter', 'reviewer', 'reviewers', 'version', 'versions', 'branch',
    'branches', 'master', 'head', 'in', 'on', 'for', 'to', 'with', 'by',
    'from', 'of', 'a', 'an', 'is', 'was', 'commit', 'issue', 'faulty', 'core',
}
NAME_WORD = re.compile(r"^[^\W\d_][\w'’.-]*$")
PARTICLES = {'de', 'van', 'von', 'da', 'le', 'la', 'del', 'der', 'den', 'di', 'du', 'dos'}
TRAILER_LINE = re.compile(r'^\s*[A-Za-z][\w-]*:\s')
LINE_START = re.compile(
    r'^(Reviewed|Reported|Tested|Diagnosed|Suggested|Review|Reviews|Report|'
    r'Reports|Patch|Original|Author|Per|Bug|Back-?patch|Analysis|Diagnosis|Test|'
    r'Testing|Idea|Initial|Minor|Some|Additional|Further|Also|Issue|Faulty|'
    r'Confirmed|Documentation|Docs|Cosmetic|Small|Slight|Heavily|Lightly|'
    r'Various|Several|Fix|Fixes|Typo|Code|Core|Same|Mostly|Extracted|First|'
    r'Second|Thanks)\b')
MENTIONED = re.compile(
    rf"\b[{UPPER}][\w'’.-]+(?:\s+(?:(?:de|van|von|da|le|la|del|der|den|di|du|dos)\s+)?[{UPPER}][\w'’-]+)+")

LEAD_FILLER = re.compile(
    r'^(?:(?:along with|with|plus|and|but|though|per|after|partly|following|'
    r'loosely based on|based on|while|also|a good bit of|a bit of|a few|a|an|'
    r'the|some|few|many|various|additional)\s+)+', re.I)
TRAIL_FILLER = re.compile(r'\s+(?:added|written|stolen|is|done)$', re.I)


def gloss(role):
    g = ' '.join(role.split()).strip(' ,.;:()')
    previous = None
    while g != previous:
        previous = g
        g = TRAIL_FILLER.sub('', LEAD_FILLER.sub('', g)).strip(' ,.;:()')
    if re.match(r"[A-Z][a-z]+\b(?!['’])", g):
        g = g[:1].lower() + g[1:]
    return g if g and g.lower() not in ('help', 'contributions', 'contribution') else None


def people(text, committer, strict=True):
    """
    Split "A, B <b@x> (docs) and me" into names, keeping emails and glosses
    as written; None if it doesn't look like a list of names.
    """
    text = text.strip(' .,;:')
    text = re.split(r'\s+(?:noticed|in|on|who|which|while|when|after|during|that|to|for|as|at|via|since|because|but|whose|per|incorporating)\b', text)[0]
    protected = re.sub(r'"[^"]*"|\([^)]*\)', lambda m: m.group().replace(',', '\x00'), text)
    parts = [p.strip().replace('\x00', ',') for p in re.split(r'\s*,\s*(?:and\s+)?|\s+and\s+|\s*&\s*', protected) if p.strip()]
    # "Tsunakawa, Takayuki" is one person
    merged = []
    for p in parts:
        if (merged and len(p.split()) == 1 and len(merged[-1].split()) == 1
                and ',' not in merged[-1] and p[0].isupper() and merged[-1][0].isupper()):
            merged[-1] += ', ' + p
        else:
            merged.append(p)
    out = []
    for p in merged:
        p = re.sub(r'^by\s+', '', p)
        mm = re.fullmatch(r'(?:me|myself|my)\b\s*(\([^)]*\))?.*|I(?:\s*(\([^)]*\)))?', p, re.I)
        if mm:
            g = mm.group(1) or (mm.group(2) if mm.lastindex and mm.lastindex >= 2 else None)
            out.append(f'{committer} {g}' if g and not re.search(r'\b(by|from)\b', g) and not re.match(r'\([A-Z]', g) else committer)
            continue
        if NOT_PEOPLE.match(p):
            continue
        m = re.fullmatch(r'(.*?)\s*(\([^)]*\))', p)
        name, g = (m[1], m[2]) if m else (p, '')
        name = re.sub(r"(’|')s$", '', name).strip()
        check = re.sub(r"\s*<[^>]*>", '', name).strip('"')
        words = check.replace(',', ' ').split()
        if not words or len(words) > 5:
            return None
        if any(w.lower() in NOT_NAMES for w in words):
            return None
        if not all(NAME_WORD.match(w) or w.lower() in PARTICLES for w in words):
            return None
        first = words[0][0]
        if strict and first.islower() or len(words) == 1 and len(words[0]) < 3 and first.isascii():
            return None
        out.append(f'{name} {g}'.strip())
    return out


def sentences(paragraph):
    lines = [l.strip() for l in paragraph.split('\n') if l.strip() and not TRAILER_LINE.match(l)]
    text = ''
    for i, l in enumerate(lines):
        if i and LINE_START.match(l) and not re.search(r'(\bby|,|\band|[.;:])$', text):
            text += '. '
        else:
            text += ' '
        text += l
    text = ' '.join(text.split())
    # adverbial parentheticals: "reviewed (in earlier versions) by X"
    text = re.sub(r'\s*\((?:in|more|less|much|mostly|partly|mainly|earlier|later|at least|among others|and others)\b[^)]*\)', '', text)
    # asides, not credit
    text = re.sub(r'\s*\((?:who|which|per|see|cf|i\.e|e\.g|I|we|as|part|extracted|via)\b[^)]*\)', '', text)
    # "(cosmetic tweaking by me)": a credit of its own
    text = re.sub(r'\s*\(([^)]*\b(?:by|from)\b[^)]*)\)', r'; \1', text)
    return [s for s in re.split(r'(?<![\s.][A-Z])(?<=[^\W\d_]{2})\.\s+|(?<![\s.][A-Z])(?<=[\w)>"])[.;]\s+|;\s+', text) if s.strip()]


FIRST_SPLIT = re.compile(
    rf',\s+(?!and\s+(?:[{UPPER}]|me\b|myself\b))(?=(?:and\s+)?[a-z])|\s+(?=with\s)|\s+(?=plus\s)|,\s+(?=Per\b)|'
    r'(?<=[a-z])\s+(?=(?:based\s+(?:on|upon)|following|per)\s)')
AND_NEXT = re.compile(
    r'(?:initial|original|patch|fix|review|reviewed|test|tests|tested|report|'
    r'analysis|diagnosis|some|minor|significant|slight|slightly|a few|various|'
    r'additional|further|incorporating|then)\b|(?:\w+ly\s+|a bit\s+)?[a-z]+ed\b')


def clauses(sent):
    out = []
    for piece in FIRST_SPLIT.split(sent):
        piece = piece.strip()
        # "reviewed by X and slightly revised by me": split at "and" once the
        # left side has credited someone
        start = 0
        for m in re.finditer(r'\s+and\s+', piece):
            left, right = piece[start:m.start()], piece[m.end():]
            if re.search(r'\b(by|from)\s+\S', left) and AND_NEXT.match(right):
                out.append(left.strip()); start = m.end()
        out.append(piece[start:].strip())
    return [c for c in out if c]


PREP = re.compile(r'^(?P<role>.*?)\b(?P<prep>by|from|of)\s+(?P<who>.+)$')
CREDIT_SENTENCE = re.compile(
    r'^(?:Per\s+(?:a\s+|the\s+)?(?:bug|reports?|gripes?|complaints?|suggestions?)\b|'
    r'(?:Bug\s+)?Reported\s+by\b|Reports?\s+(?:by|from)\b|Reviewed\s+by\b|Reviews?\s+(?:by|from)\b|'
    r'Tested\s+by\b|Diagnosed\s+by\b|Diagnosis\s+by\b|Noticed\s+by\b|Spotted\s+by\b|Pointed out by\b)')
COLON = re.compile(r'^(?P<role>[A-Za-z][A-Za-z ,&/-]*?):\s*(?P<who>.+)$')


def roles_for(role, names, committer, previous):
    if COMMIT_REF.match(role):
        return [], 'skip'
    if CONNECTIVE.match(role) and previous:
        return [(t, n, g) for t, _, g in previous for n in names], 'ok'
    tags = [t for t, pat in ROLE_WORDS if re.search(pat, role.strip(), re.I)]
    if re.search(r'(suggested|proposed)\s+(fix|patch|change)|suggestion', role, re.I) and not re.search(r'\b(and|&)\s+(patch|fix|code)\b', role, re.I):
        # a suggested fix is a suggestion, not authorship
        tags = [t for t in tags if t != 'Author']
        if 'Suggested-by' not in tags:
            tags.append('Suggested-by')
    elif 'Suggested-by' in tags and not re.search(r'\b(and|&)\s+(patch|fix|code)\b|\b(patch|fix|code)\s+(and|&)', role, re.I):
        # "fix suggestions from": the idea, not the patch; "idea and patch": both
        tags = [t for t in tags if t != 'Author']
    # "the patch series has also had review from": review or testing of a
    # patch doesn't make anyone an author
    if {'Reviewed-by', 'Tested-by'} & set(tags):
        tags = [t for t in tags if t != 'Author']
    # wording/doc/comment suggestions on a patch are review, not the idea
    if 'Suggested-by' in tags and re.search(r'(word|doc|comment|language|naming|phrasing|text)\w*\s+suggest|suggested\s+(language|wording|text)', role, re.I):
        return [('Reviewed-by', n, gloss(role)) for n in names], 'ok'
    if 'Author' in tags and EDITING.search(role):
        tags.remove('Author')
    out = []
    if re.search(r'kibitz|kabitz', role, re.I):
        out += [('Co-authored-by', n, gloss(role)) for n in names]
        tags = [t for t in tags if t != 'Author']
        if not tags:
            return out, 'ok'
    if 'Author' in tags and EARLIER.search(role):
        tags.remove('Author')
        out += [('Co-authored-by', n, gloss(role)) for n in names]
    if names == [committer] and re.search(r'\bfix\b|beautif', role, re.I) and 'Author' not in tags:
        return out + [('Co-authored-by', n, gloss(role)) for n in names], 'ok'
    if EDITING.search(role) and (not tags or tags == ['Tested-by'] and names == [committer]):
        return out + [('Co-authored-by', n, gloss(role)) for n in names], 'ok'
    if not {'Reported-by', 'Diagnosed-by'} & set(tags) or len(tags) > 1:
        # "(bug #N)" after a name belongs on the report, not on the patch
        def strip_bug(n):
            return re.sub(r'\s*\(bug\s*#\s*\d+\)', '', n)
    else:
        def strip_bug(n):
            return n
    for t in tags:
        g = None
        if t == 'Reported-by':
            b = re.search(r'bug\s*#\s*\d+', role, re.I)
            g = re.sub(r'bug\s*#\s*', 'bug #', b.group(), flags=re.I) if b else None
        out += [(t, n if t in ('Reported-by', 'Diagnosed-by') else strip_bug(n), g) for n in names]
    if not out and gloss(role):
        out = [('Co-authored-by', n, gloss(role)) for n in names]
    return out, 'ok' if out else 'unknown'


def parse(paragraph, committer):
    """([(tag, person, gloss)], confident, [sentences used], [reasons])"""
    result, used, reasons = [], [], []
    for sent in sentences(paragraph):
        previous = None
        for ci, clause in enumerate(clauses(sent)):
            m = PREP.match(clause) or COLON.match(clause)
            if m:
                role, who = m['role'], m['who']
                names = people(who, committer)
                inner = list(re.finditer(r'\b(?:by|from|of)\s+', who))
                if names is None and inner and people(who[inner[-1].end():], committer):
                    dropped = who[:inner[-1].start()]
                    if re.search(r'\b[A-Z][a-z]', dropped):
                        reasons.append(('dropped name', clause))
                    role = role + ' ' + m['prep'] + ' ' + dropped
                    who = who[inner[-1].end():]
                    names = people(who, committer)
                if names is None:
                    if re.search(r'\b(by|from)\s+[A-Z]', clause):
                        reasons.append(('names not parsed', clause))
                    continue
                if not names:
                    continue
                got, status = roles_for(role, names, committer, previous)
                if status == 'unknown':
                    reasons.append(('unknown role', clause))
                if got:
                    result += got; previous = got; used.append(sent)
            elif (pm := re.match(r"^(?:[Pp]er|[Ff]rom)\s+(.+?)(?:'|’)s\s+(?:bug\s+)?(report|complaint|gripe|suggestion|idea|analysis)", clause)):
                names = people(pm[1], committer)
                if names:
                    t = {'suggestion': 'Suggested-by', 'idea': 'Suggested-by', 'analysis': 'Diagnosed-by'}.get(pm[2], 'Reported-by')
                    got = [(t, n, None) for n in names]
                    result += got; previous = got; used.append(sent)
            elif (im := re.match(r"^I\s+(?:\([^)]*\)\s+)?(?:(?:also|then|just|additionally|further|later|separately|\w+ly)\s+)*"
                                 r"(rewrote|re-wrote|added|fixed|adjusted|cleaned up|cleaned|tweaked|reworked|changed|edited|"
                                 r"editorialized|revised|removed|modified|expanded|extended|wrote|updated|improved|polished|"
                                 r"simplified|reformatted|reorganized|renamed|moved|refactored|corrected|rephrased|reworded|"
                                 r"documented|hacked|whacked|massaged|did(?!n|\s+not))\b\s*(.*)$", clause)):
                rest = im[2]
                g = gloss(rest)
                if not re.match(r'(the|a|an|some|his|her|their|its|this|that|these|those|all|most|several|many|more)\b', rest, re.I) \
                        or (g or '').lower() in ('problem', 'issue', 'bug', 'rest', 'it', 'this', 'that', 'them', 'same') \
                        or re.search(r'\b(problem|issue|bug|mistake|typo|oversight)s?\b', g or '', re.I):
                    g = gloss(f'{im[1]} {rest}')
                got = [('Co-authored-by', committer, g)]
                result += got; previous = got; used.append(sent)
            else:
                lead = re.sub(r'^[Pp]er\s+', '', clause)
                names = people(lead, committer)
                if not names:
                    continue
                if lead != clause:
                    got = [('Reported-by', n, None) for n in names]
                elif ci == 0:
                    got = [('Author', n, None) for n in names]
                else:
                    continue
                result += got; previous = got; used.append(sent)
    used = list(dict.fromkeys(used))
    # everyone the sentences name must have been given a tag
    tagged = {fold(bare(n)) for _, n, _ in result} | {fold(n) for _, n, _ in result}
    for s in used:
        for nm in MENTIONED.findall(s):
            if any(w.lower() in NOT_NAMES for w in nm.split()):
                continue
            f = fold(nm)
            if not any(f in t or t in f for t in tagged):
                reasons.append(('unassigned name', nm))
        if re.search(r'\b(me|myself)\b', s) and fold(committer) not in tagged:
            reasons.append(('unassigned me', s))
    if not result:
        reasons.append(('nothing parsed', ''))
    return result, not reasons, used, reasons



if __name__ == '__main__':
    repo, pgsql, dst = sys.argv[1:4]
    sys.path.insert(0, repo)
    import commitdata
    data = commitdata.load(repo)
    proposals, seen = [], set()
    for f in data.tag_fixes:
        if f.verb != 'insert' or f.tag != 'Author' or f.hash in seen:
            continue          # only the first inserted Author is the legacy credit
        seen.add(f.hash)
        c = data.commits[f.hash]
        msg = subprocess.run(['git', '-C', pgsql, 'log', '-1', '--format=%B', f.hash],
                             capture_output=True, text=True, check=True).stdout
        paras = [p for p in re.split(r'\n\s*\n', '\n'.join(msg.split('\n')[1:])) if p.strip()]

        def body(p):
            return '\n'.join(l for l in p.split('\n') if not TRAILER_LINE.match(l))
        target = fold(bare(f.value)).split()[-1]
        mine = [p for p in paras if target in fold(body(p))]
        if not mine:
            continue
        extra = []
        for p in paras:
            if p is mine[-1]:
                continue
            for s in sentences(p):
                if CREDIT_SENTENCE.match(s):
                    extra.append(s.rstrip('.') + '.')
        tags, confident, used, reasons = parse(mine[-1], c.committer)
        for e in extra:          # credit sentences elsewhere: used only if they parse cleanly
            etags, _, eused, ereasons = parse(e, c.committer)
            if etags and not [r for r in ereasons if r[0] != 'nothing parsed']:
                tags += [t for t in etags if t not in tags]; used += [u for u in eused if u not in used]
        proposals.append(dict(year=c.year, hash=f.hash, committer=c.committer,
                              legacy=f.value, para=' '.join(body(mine[-1]).split()),
                              tags=tags, confident=confident, used=used, reasons=reasons))
    json.dump(proposals, open(dst, 'w'), ensure_ascii=False, indent=1)
