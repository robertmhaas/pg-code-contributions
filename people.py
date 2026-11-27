"""
Map the names and addresses in commits and mail to people.

    import people
    p = people.load()
    p.resolve_value('Jane Doe <jd@example.com>')   # (person id or None, status)
    p.resolve('Jane Doe', 'jd(at)example(dot)com')  # the same, from a name and address
    p.persons['Jane Doe']                          # Person

People are found by linking identifiers: an address, or a name reduced to a
key.  A name and an address that appear together in a mail From or a tag
value are the same person.  overrides/people.tsv pins identifiers to people
by hand; see README.md.

Each person tag is expected to name one person under a canonical key; tag
fixes make them so.  This module only reads the name and address in a
value.
"""

import collections
import dataclasses
import glob
import os
import re
import unicodedata

import commitdata

PEOPLE_COLUMNS = ['person', 'identifier']
MAIL_COLUMNS = ['message_id', 'sent_at', 'from_name', 'from_address']
NOBODY = '-'

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


def identifier_node(identifier):
    """The node an overrides identifier names: ('a', address) or ('n', key)."""
    a = address_key(identifier) if ADDRESS.fullmatch(identifier.strip().strip('<>')) else None
    if a:
        return ('a', a)
    nodes = name_nodes(clean_name(identifier))
    return nodes[0] if nodes else None


@dataclasses.dataclass
class Person:
    id: str                     # the display name, unique
    pinned: bool                # named in overrides/people.tsv
    nodes: set
    own_names: collections.Counter  # as written by them: git author, mail
    tag_names: collections.Counter  # as written in others' commit tags


@dataclasses.dataclass
class Pin:
    person: str
    identifier: str
    node: tuple
    where: str


class People:
    def __init__(self):
        self.persons = {}       # id: Person
        self.node_person = {}   # node: person id, or NOBODY
        self.ambiguous = {}     # node: sorted person ids it would join
        self.pins = []
        self.problems = []      # found while loading; check-people.py reports them
        self.seen = set()       # nodes that occur in the data

    def node_for(self, name, address):
        """The address's node if there is one with an answer, else the name's."""
        nodes = []
        a = address_key(address) if address else None
        if a:
            nodes.append(('a', a))
        nodes += name_nodes(name)
        for node in nodes:
            if node in self.node_person and self.node_person[node] != NOBODY:
                return node
        for node in nodes:
            if node in self.node_person or node in self.ambiguous:
                return node
        return nodes[0] if nodes else None

    def resolve(self, name, address=None):
        """
        Return (person id, 'ok'), or (None, status) where status is
        'nobody' (pinned to "-"), 'ambiguous' (would join two pinned
        people), 'single name' (one word, not pinned) or 'unknown'.
        """
        node = self.node_for(name, address)
        if node is None:
            return None, 'unknown'
        if node in self.ambiguous:
            return None, 'ambiguous'
        person = self.node_person.get(node)
        if person == NOBODY:
            return None, 'nobody'
        if person:
            return person, 'ok'
        return None, 'single name' if node[0] == 's' else 'unknown'

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


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def mail_rows(root):
    for path in sorted(glob.glob(os.path.join(root, 'data', 'mail', '*', '*.tsv'))):
        yield from commitdata.read_tsv(path, MAIL_COLUMNS, root)


def load(root=commitdata.ROOT, data=None):
    """Build People from data/ and overrides/people.tsv."""
    data = data or commitdata.load(root)
    people = People()
    own_names = collections.defaultdict(collections.Counter)   # node: written names
    tag_names = collections.defaultdict(collections.Counter)
    edges = set()
    single_edges = set()    # one-word names and their addresses; used if pinned
    nodes = set()
    seen = set()

    def see(name, address, counter):
        a = address_key(address) if address else None
        named = name_nodes(name)
        seen.update(named)
        linked = [n for n in named if n[0] == 'n']
        single = [n for n in named if n[0] == 's']
        if linked:
            counter[linked[0]][name] += 1
        elif single:
            # Kept for the display name of someone known by no full name.
            counter[('a', a) if a else single[0]][name] += 1
            if a:
                single_edges.add((single[0], ('a', a)))
        if a:
            linked.append(('a', a))
        nodes.update(linked)
        for x, y in zip(linked, linked[1:]):
            edges.add((x, y))

    for c in data.commits.values():
        see(clean_name(c.git_author), None, own_names)
        for tag, value in c.tags:
            if is_person_tag(tag) and not looks_like_list(value):
                see(*parse_value(value), tag_names)
    for _, (_, _, from_name, from_address) in mail_rows(root):
        see(clean_name(from_name), from_address, own_names)

    # Pins.  The person column is a name too, and is pinned along with
    # the identifier; if two people's names share a key, that name is
    # ambiguous, as it would be in the data.
    pinned = collections.defaultdict(set)
    explicit = collections.defaultdict(set)
    path = os.path.join(root, 'overrides', 'people.tsv')
    if os.path.exists(path):
        for where, (person, identifier) in commitdata.read_tsv(
                path, PEOPLE_COLUMNS, root, comments=True):
            node = identifier_node(identifier)
            if node is None:
                people.problems.append(f'{where}: no name or address in {identifier!r}')
                continue
            people.pins.append(Pin(person, identifier, node, where))
            explicit[node].add(person)
            pinned[node].add(person)
            if person != NOBODY:
                label = identifier_node(person)
                if label and label[0] == 'n':   # a one-word name pins nothing
                    pinned[label].add(person)
    people.seen = nodes | seen
    for node, persons in explicit.items():
        if len(persons) > 1:
            people.problems.append(
                f'{node[1]!r} is pinned to more than one person: {sorted(persons)}')

    nobody = {n for n, ps in pinned.items() if NOBODY in ps}
    ambiguous = {n for n, ps in pinned.items() if len(ps) > 1 and n not in nobody}
    label_of = {n: next(iter(ps)) for n, ps in pinned.items()
                if len(ps) == 1 and n not in nobody}
    for n in ambiguous:
        people.ambiguous[n] = sorted(pinned[n])
    for n in nobody:
        people.node_person[n] = NOBODY
    nodes |= set(label_of)
    # A one-word name that's pinned is as good as a full name, so it links
    # the addresses it's sent from: "MauMau <maumau307@...>".
    edges |= {e for e in single_edges if e[0] in label_of}

    blocked = nobody | ambiguous
    adjacency = collections.defaultdict(set)
    uf = _UnionFind()
    for n in nodes - blocked:
        uf.find(n)
    for a, b in edges:
        if a not in blocked and b not in blocked:
            adjacency[a].add(b)
            adjacency[b].add(a)
            uf.union(a, b)
    first = {}
    for n, label in label_of.items():
        if label in first:
            uf.union(first[label], n)
        else:
            first[label] = n

    components = collections.defaultdict(set)
    for n in nodes - blocked:
        components[uf.find(n)].add(n)

    groups = []                 # (label or None, nodes)
    for members in components.values():
        labels = {label_of[n] for n in members if n in label_of}
        if len(labels) <= 1:
            groups.append((next(iter(labels), None), members))
            continue
        # The component would join people pinned apart.  Each unpinned
        # identifier goes to the person whose pinned identifiers reach it
        # without passing through another person's; if more than one
        # reaches it, it's ambiguous.
        reached = collections.defaultdict(set)
        for label in labels:
            seen = {n for n in members if label_of.get(n) == label}
            todo = list(seen)
            while todo:
                for m in adjacency[todo.pop()]:
                    if m not in seen and m not in label_of:
                        seen.add(m)
                        todo.append(m)
            for n in seen:
                reached[n].add(label)
        for label in labels:
            groups.append((label, {n for n in members
                                   if label_of.get(n) == label
                                   or (n not in label_of and reached[n] == {label})}))
        for n in members:
            if n not in label_of and len(reached[n]) > 1:
                people.ambiguous[n] = sorted(reached[n])

    taken = {}
    groups.sort(key=lambda g: g[0] is None)     # pinned people keep their names
    for label, members in groups:
        person_own = sum((own_names[n] for n in members), collections.Counter())
        person_tags = sum((tag_names[n] for n in members), collections.Counter())
        if label:
            pid = label
        else:
            addresses = sorted(obfuscate(n[1]) for n in members if n[0] == 'a')
            full = display_name(person_own, person_tags)
            pid = full or one_word_name(person_own, person_tags) or (addresses or sorted(
                n[1] for n in members))[0]
            if pid in taken:
                if full:
                    people.problems.append(f'two people would be called {pid!r}')
                pid = f'{pid} <{addresses[0]}>' if addresses else f'{pid} (2)'
        taken[pid] = True
        person = people.persons.get(pid)
        if person is None:
            person = people.persons[pid] = Person(
                pid, bool(label), set(), collections.Counter(), collections.Counter())
        person.nodes |= members
        person.own_names += person_own
        person.tag_names += person_tags
        for n in members:
            people.node_person[n] = pid
    return people


def obfuscate(address):
    return address.replace('@', '(at)').replace('.', '(dot)')


def one_word_name(own_names, tag_names):
    """
    For someone known by no full name, the one-word name they use most,
    such as "didier" or "BharatDB", or failing that the one commits use.
    """
    for counter in (own_names, tag_names):
        words = collections.Counter({n: c for n, c in counter.items()
                                     if n and single_key(n) and not ADDRESS.search(n)})
        if words:
            return max(words, key=lambda n: (words[n], n != n.lower(), n))
    return None


def display_name(own_names, tag_names):
    """
    Choose how to write an unpinned person's name: as they write it
    themselves, as git author or in mail, or failing that as commit tags
    do, preferring the Latin alphabet either way.  A name in two scripts
    counts as its Latin part.  The most used spelling wins; among those
    differing only in accents or case, one that isn't all upper or lower
    case (taken from commits if need be), then one with accents.
    """
    def latin(n):
        parts = script_parts(n)
        return parts[0] if parts and name_key(parts[0]) else n

    pools = []
    for counter in (own_names, tag_names):
        counter = collections.Counter({latin(n): c for n, c in counter.items() if name_key(n)})
        pools.append(collections.Counter({n: c for n, c in counter.items()
                                          if not OTHER_LETTER.search(n)}))
        pools.append(counter)
    pools = [pools[0], pools[2], pools[1], pools[3]]
    for counter in pools:
        if not counter:
            continue
        ordered = collections.defaultdict(collections.Counter)
        for n, count in counter.items():
            ordered[' '.join(re.findall(r'\w+', fold(n)))][n] += count
        spelling = max(ordered.items(), key=lambda kv: (sum(kv[1].values()), kv[0]))[0]

        def variants(c):
            return collections.Counter({latin(n): k for n, k in c.items()
                                        if ' '.join(re.findall(r'\w+', fold(latin(n)))) == spelling})
        found = variants(counter)
        if all(n in (n.lower(), n.upper()) for n in found):
            found += variants(tag_names)     # borrow capitals from commits
        return max(found, key=lambda n: (n != n.lower() and n != n.upper(), not n.isascii(),
                                         found[n], n))
    return None
