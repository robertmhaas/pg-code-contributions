"""
The people named in commits and mail, as listed in people.txt.

    import people
    p = people.load()
    p.resolve_value('Jane Doe <jd@example.com> (test case)')
                            # (person id or None, status)
    p.resolve('Jane Doe', 'jd(at)example(dot)com')   # the same, from a name and address
    p.persons[pid]          # Person: id, name, address, kind, identities
    p.authors(commit)       # [(value, person id, status)] for commit.authors,
                            # without those that identify no one, such as AI tools
    p.primary_author(commit)   # the first of those, credited with the lines

A person id is the stanza's Canonical: value, so it changes if that's
edited.  update-people.py adds what new commits and mail bring, and people
edit the file by hand to merge, split or rename; see README.md.  This
module only reads it.

An address belongs to one person.  A name usually does; when several
people have it, it identifies none of them on its own.  Names are compared
by a key that ignores case, accents, punctuation, word order, and initials
if two other words are left, so "Zhou, Zhiguo" and "zhiguo zhou" match, as
do "Álvaro Herrera" and "Alvaro Herrera", and "Andrey M. Borodin" and
"Andrey Borodin".  Addresses, quotes, and glosses or affiliations in
trailing parentheses are removed first, and a name written in two scripts,
like "Hou Zhijie 侯志杰" or "王海洋 (Haiyang Wang)", also counts as each
script's part.

A name and address are looked up like this:

- An address listed for someone is theirs, whatever the name.
- Otherwise the name decides: a stanza listing it on its own first, then
  one listing it with an address.
- A one-word name, like "MauMau" or "Michael", identifies someone only
  where it's listed on its own.  So a handle can be listed, but a bare
  first name in a commit tag identifies no one unless a tag fix spells it
  out.

A person tag is one of PERSON_TAGS and names one person; tag fixes make
them so.  A value that still names several, like "Jane Doe and John Roe",
identifies no one.  (When the only separators are commas and there's an
address, it takes two full names to make a list, so
"Wong, Yi Wen <yw@example.com>" is one person.)
"""

import collections
import dataclasses
import datetime
import glob
import os
import re
import unicodedata

import commitdata

MAIL_COLUMNS = ['message_id', 'sent_at', 'from_name', 'from_address']
REGISTRY = 'people.txt'
REGISTRY_KEYS = ('Canonical', 'Alternate', 'Kind', 'Distinct')
KINDS = ('none',)

# Letters NFKD doesn't take apart.
FOLD = str.maketrans({'ø': 'o', 'ł': 'l', 'đ': 'd', 'ð': 'd', 'þ': 'th',
                      'æ': 'ae', 'œ': 'oe', 'ß': 'ss', 'ı': 'i'})
CJK = re.compile(r'[぀-ヿ㐀-鿿가-힯]')
ADDRESS = re.compile(r'(?:[\w.+=-]|\(dot\))+(?:@|\(at\))[\w-]+(?:(?:\.|\(dot\))[\w-]+)+', re.I)
LATIN = re.compile(r'[A-Za-z\u00c0-\u024f\u1e00-\u1eff]')
OTHER_LETTER = re.compile(r'(?![\d_])(?![A-Za-z\u00c0-\u024f\u1e00-\u1eff])\w')
LIST_SEPARATOR = re.compile(r'\s*(?:,\s*(?:and\b)?|;|&|\+|\band\b)\s*', re.I)


PERSON_TAGS = ('Author', 'Co-authored-by', 'Reviewed-by', 'Reported-by', 'Tested-by',
               'Suggested-by', 'Diagnosed-by')


tag_key = commitdata.tag_key


def is_person_tag(tag):
    """Whether a tag is one of PERSON_TAGS, ignoring case and spacing."""
    return tag_key(tag) in {t.lower() for t in PERSON_TAGS}


def address_key(address):
    """Lowercased, with any (at)/(dot) obfuscation undone; None if not an address."""
    a = address.strip().strip('<>').replace('(at)', '@').replace('(dot)', '.').lower()
    return a if re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', a) else None


def fold(text):
    text = unicodedata.normalize('NFKD', text.casefold().translate(FOLD))
    return ''.join(c for c in text if not unicodedata.combining(c))


def name_words(name):
    """
    Folded words of a name, each a list of its letters-and-digits runs.
    Initials are dropped if at least two other words are left, so "Andrey
    M. Borodin" is two words but "Vignesh C" keeps its C.  A word is
    delimited by spaces, so a handle such as "suyu.cmj" is one word.
    """
    words = [w for w in (re.findall(r'\w+', w) for w in fold(name).split()) if w]
    full = [w for w in words if any(len(t) > 1 or CJK.match(t) for t in w)]
    return full if len(full) >= 2 else words


def name_key(name):
    """
    The key two spellings of one name share: folded, without initials or
    punctuation, words sorted, so "Zhou, Zhiguo", "Zhiguo Zhou" and
    "zhiguo zhou" match, as do "Álvaro" and "Alvaro".  None if the name
    is a single word, which is too weak to link anything; see single_key.
    """
    words = name_words(name)
    if len(words) >= 2 or (len(words) == 1 and len(words[0]) == 1
                           and len(words[0][0]) >= 2 and CJK.match(words[0][0])):
        return ' '.join(sorted(t for w in words for t in w))
    return None


def single_key(name):
    """Key for a one-word name such as "MauMau", which counts only if pinned."""
    words = name_words(name)
    return ' '.join(words[0]) if len(words) == 1 else None


def script_parts(name):
    """
    For a name written in two scripts, as in "Hou Zhijie 侯志杰", the
    Latin words and the others: ['Hou Zhijie', '侯志杰'].  Otherwise [].
    A word mixing scripts belongs to neither.
    """
    words = name.replace('/', ' ').split()
    latin = [w for w in words if LATIN.search(w) and not OTHER_LETTER.search(w)]
    other = [w for w in words if OTHER_LETTER.search(w) and not LATIN.search(w)]
    return [' '.join(latin), ' '.join(other)] if latin and other else []


def name_nodes(name):
    """The nodes for a written name: its own key, plus each script's part."""
    if not name or ADDRESS.search(name):
        return []
    key = name_key(name)
    if key is None:
        single = single_key(name)
        return [('s', single)] if single else []
    nodes = [('n', key)]
    for part in script_parts(name):
        k = name_key(part)
        if k and ('n', k) not in nodes:
            nodes.append(('n', k))
    return nodes


def clean_name(text):
    """
    The name in a value, without addresses, glosses or affiliations in
    trailing parentheses, or surrounding quotes, and with "Last, First"
    turned around.
    """
    text = re.sub(r'<[^<>]*>', ' ', text)
    text = ADDRESS.sub(' ', text)
    while True:
        m = re.search(r'\s*(\([^()]*\)|\[[^\[\]]*\])\s*$', text)
        if not m:
            break
        text, inner = text[:m.start()], m.group(1)[1:-1].strip()
        if (not LATIN.search(text) and OTHER_LETTER.search(text)
                and re.fullmatch(r'[A-Za-z\u00c0-\u024f.\'-]+(?: [A-Za-z\u00c0-\u024f.\'-]+)+', inner)):
            text = f'{text} {inner}'    # "王海洋 (Haiyang Wang)": the same name, transliterated
            break
    text = ' '.join(text.split()).strip(' "\'`')
    m = re.fullmatch(r'([^,]+),\s*([^,]+)', text)
    if m:
        text = f'{m.group(2)} {m.group(1)}'
    return text.strip(' "\'`')


def parse_value(value):
    """(name, address) of the person in a tag value."""
    address = re.findall(r'<([^<>]*@[^<>]*)>', value) or ADDRESS.findall(value) or [None]
    return clean_name(value), address[0]


def looks_like_list(value):
    """
    Whether a tag value names several people, as in "Jane Doe and John
    Roe"; tag fixes should split those.  Separators inside glosses,
    addresses and quotes don't count, and if the only separators are
    commas and there's an address, as in "Wong, Yi Wen <yw@example.com>",
    it takes two full names to make a list.
    """
    masked = re.sub(r'\([^()]*\)|<[^<>]*>|"[^"]*"', lambda m: 'x' * len(m.group()), value)
    masked = ADDRESS.sub(lambda m: 'x' * len(m.group()), masked)
    separators = [m.group().strip().lower() for m in LIST_SEPARATOR.finditer(masked)]
    parts = [q for q in LIST_SEPARATOR.split(masked) if q.strip()]
    full = sum(1 for q in parts if name_key(clean_name(q)))
    if len(parts) < 2 or full == 0:
        return False
    return full >= 2 or not all(x == ',' for x in separators) or not (
        ADDRESS.search(value) or '<' in value)


def obfuscate(address):
    return address.replace('@', '(at)').replace('.', '(dot)')



def parse_identity(value):
    """(name, address) from "Name <address>", "Name" or "<address>"."""
    m = re.fullmatch(r'(.*?)\s*<([^<>]*)>\s*', value)
    if m:
        return m.group(1).strip(), address_key(m.group(2))
    return value.strip(), None


def identity_text(name, address):
    """The inverse of parse_identity, with the address written as in data/mail."""
    return ' '.join(x for x in (name, f'<{obfuscate(address)}>' if address else '') if x)


@dataclasses.dataclass
class Person:
    id: str             # its Canonical value, as written; unique
    name: str           # the name to show it by
    address: str        # its canonical address, or None
    kind: str           # 'person', or 'none' for what isn't one
    identities: list    # [(name, address)], the Canonical first
    where: str          # file:line of the stanza
    distinct: list = dataclasses.field(default_factory=list)   # [(name, address)] of others it isn't

    def is_not(self, other):
        """Whether this stanza says it isn't the other person."""
        names = {n for name, _ in other.identities for n in name_nodes(name)}
        addresses = {a for _, a in other.identities if a}
        return any((a in addresses) if a else bool(set(name_nodes(n)) & names)
                   for n, a in self.distinct)


class People:
    def __init__(self):
        self.persons = {}                            # id: Person
        self.by_address = {}                         # address: id
        self.by_name = collections.defaultdict(set)  # name node: ids
        self.by_bare_name = collections.defaultdict(set)   # ... in identities without an address
        self.pairs = collections.defaultdict(set)    # id: {(name node, address)}
        self.problems = []

    def add_person(self, person):
        if person.id in self.persons:
            self.problems.append(f'{person.where}: another person is {person.id!r}')
            return
        self.persons[person.id] = person
        for name, address in person.identities:
            self.index(person.id, name, address, person.where)

    def index(self, pid, name, address, where=None):
        nodes = name_nodes(name)
        if address:
            other = self.by_address.setdefault(address, pid)
            if other != pid:
                self.problems.append(f'{where}: {obfuscate(address)} belongs to {other!r} too')
        for node in nodes:
            self.by_name[node].add(pid)
            if not address:
                self.by_bare_name[node].add(pid)
            self.pairs[pid].add((node, address))
        if not nodes and address:
            self.pairs[pid].add((None, address))

    def answer(self, pid):
        if self.persons[pid].kind == 'none':
            return None, 'nobody'
        return pid, 'ok'

    def find(self, name, address=None):
        """
        (person id, None) for the stanza a name and address belong to,
        whatever its Kind, or (None, status) as for resolve().
        """
        a = address_key(address) if address else None
        if a in self.by_address:
            return self.by_address[a], None
        nodes = name_nodes(name)
        for node in nodes:
            # A one-word name counts only where it's listed on its own.
            for index in (self.by_bare_name,) if node[0] == 's' else (self.by_bare_name, self.by_name):
                ids = index.get(node)
                if ids:
                    return (next(iter(ids)), None) if len(ids) == 1 else (None, 'ambiguous')
        return None, 'single name' if any(n[0] == 's' for n in nodes) else 'unknown'

    def resolve(self, name, address=None):
        """
        Return (person id, 'ok'), or (None, status) where status is
        'nobody' (Kind: none), 'ambiguous' (a name several people have),
        'single name' (one word, not listed) or 'unknown'.  An address
        decides if it's listed; otherwise the name does, a listing of the
        name on its own winning over one with an address.  A one-word name,
        such as a handle, counts only where it's listed on its own.
        """
        pid, status = self.find(name, address)
        return self.answer(pid) if pid else (None, status)

    def resolve_value(self, value):
        """
        (person id, 'ok') for a tag value, or (None, status) as for
        resolve(), or (None, 'list') if the value names several people.
        """
        if looks_like_list(value):
            return None, 'list'
        return self.resolve(*parse_value(value))

    def authors(self, commit):
        """
        [(value, person id or None, status)] for a commit's authors (see
        commitdata.Commit.authors), leaving out any that identify no one,
        such as AI tools.  A value naming someone who can't be identified
        is kept, with person None.
        """
        return [(value, *r) for value in commit.authors
                for r in [self.resolve_value(value)] if r[1] != 'nobody']

    def primary_author(self, commit):
        """The first of authors(), who is credited with the commit's lines, or None."""
        authors = self.authors(commit)
        return authors[0] if authors else None


def read_stanzas(path):
    """
    Yield (line number, [(key, value)]) for each stanza in people.txt,
    without comments.  Stanzas are separated by blank lines.
    """
    stanza, first = [], None
    with open(path, encoding='utf-8') as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip('\n')
            if not line.strip():
                if stanza:
                    yield first, stanza
                stanza, first = [], None
            elif not line.startswith('#'):
                key, colon, value = line.partition(':')
                stanza.append((key.strip(), value.strip() if colon else None))
                first = first or n
    if stanza:
        yield first, stanza


def load(root=commitdata.ROOT, path=None):
    """Read people.txt, or the registry at path."""
    people = People()
    path = path or os.path.join(root, REGISTRY)
    rel = os.path.relpath(path, root)
    for n, stanza in read_stanzas(path):
        where = f'{rel}:{n}'
        canonical = [v for k, v in stanza if k == 'Canonical']
        bad = [k for k, v in stanza if k not in REGISTRY_KEYS or v is None]
        if bad:
            people.problems.append(f'{where}: unknown line {bad[0]!r}')
        if len(canonical) != 1:
            people.problems.append(f'{where}: needs one Canonical line, has {len(canonical)}')
            if not canonical:
                continue
        kinds = [v for k, v in stanza if k == 'Kind']
        if any(v not in KINDS for v in kinds):
            people.problems.append(f'{where}: unknown Kind {kinds}')
        name, address = parse_identity(canonical[0])
        identities = [(name, address)] + [parse_identity(v) for k, v in stanza if k == 'Alternate']
        people.add_person(Person(canonical[0], name, address, 'none' if 'none' in kinds else 'person',
                                 identities, where,
                                 [parse_identity(v) for k, v in stanza if k == 'Distinct']))
    return people


def mail_rows(root):
    for path in sorted(glob.glob(os.path.join(root, 'data', 'mail', '*', '[0-9]' * 4 + '.tsv'))):
        yield from commitdata.read_tsv(path, MAIL_COLUMNS, root)




def observations(root=commitdata.ROOT, data=None):
    """
    Yield (when, source, name, address, ref) for every person named in the
    data: each git author, each person tag value since tag fixes (lists
    left out), and each mail sender.  when is UTC; source is 'git', 'tag'
    or 'mail'; ref is the commit or Message-ID.  Names are cleaned up as
    by clean_name.
    """
    data = data or commitdata.load(root)
    for c in data.commits.values():
        when = c.committed_at.astimezone(datetime.timezone.utc)
        yield when, 'git', clean_name(c.git_author), None, c.hash
        for tag, value in c.tags:
            if is_person_tag(tag) and not looks_like_list(value):
                name, address = parse_value(value)
                yield when, 'tag', name, address_key(address) if address else None, c.hash
    for _, (message_id, sent_at, name, address) in mail_rows(root):
        when = datetime.datetime.strptime(sent_at, '%Y-%m-%d %H:%M:%S %z').astimezone(datetime.timezone.utc)
        yield when, 'mail', clean_name(name), address_key(address), message_id
