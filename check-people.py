#!/usr/bin/env python3
"""
Load people.txt and report anything that looks wrong.

Reports stanzas that don't parse, addresses listed for two people, and
names and addresses in the data that people.txt doesn't have yet (run
update-people.py).  For commits since 2011, where the tag fixes start,
also reports person tags that list several people, name someone who
can't be identified, or start with a gloss; credit keys that aren't one
of people.PERSON_TAGS; and people named twice in one tag.

With --review, also lists things to check by hand: pairs of people whose
names are near-matches, such as "Masahiko Sawada" and "Masahiko Sawasa";
names that several people have; people whose Canonical name isn't how
they've been writing it lately; and lines matching nothing in the data.
A Distinct line in either stanza keeps a pair off the first two lists.

Exits with status 1 if there are problems.
"""

import argparse
import collections
import re
import sys

import commitdata
import people

SINCE = 2011

# Keys that look like credits but aren't.
NOT_CREDITS = {'brown-paper-bagged-by'}


def check_tags(data, p):
    """Person tags since SINCE that don't identify one person cleanly."""
    bad = collections.defaultdict(list)
    for c in data.commits.values():
        if c.year < SINCE:
            continue
        for tag, value in c.tags:
            key = people.tag_key(tag)
            if people.is_person_tag(tag):
                pid, status = p.resolve_value(value)
                if pid is None and status != 'nobody':
                    bad[status, value].append(c.hash)
                elif value.lstrip().startswith(('(', '[')):
                    bad['starts with a gloss', value].append(c.hash)
            elif (key.endswith('-by') or key.startswith(('author', 'co-author'))) \
                    and key not in NOT_CREDITS:
                bad['not a canonical key', f'{tag}: {value}'].append(c.hash)
    problems = []
    for (status, text), hashes in sorted(bad.items(), key=lambda kv: (kv[0][0], -len(kv[1]))):
        if status == 'unknown':
            status = 'not in people.txt (run update-people.py)'
        more = f' and {len(hashes) - 3} more' if len(hashes) > 3 else ''
        problems.append(f'{status}: {text!r} in {", ".join(hashes[:3])}{more}')
    return problems


def check_duplicates(data, p):
    """The same person named twice in one tag of a commit."""
    problems = []
    for c in data.commits.values():
        if c.year < SINCE:
            continue
        named = collections.defaultdict(list)
        for tag, value in c.tags:
            if people.is_person_tag(tag):
                pid, _ = p.resolve_value(value)
                if pid:
                    named[people.tag_key(tag), pid].append(value)
        for (tag, pid), texts in named.items():
            if len(texts) > 1:
                problems.append(f'{c.hash}: {pid} is in {tag} {len(texts)} times: {texts}')
    return problems


def check_observations(p, observations):
    """Mail senders and git authors that people.txt doesn't have yet."""
    missing = collections.Counter()
    for when, source, name, address, ref in observations:
        if source == 'tag':
            continue        # check_tags reports those, by commit
        pid, status = p.find(name, address)
        if pid is None and status == 'unknown' and (address or people.name_key(name)):
            missing[source] += 1
    return [f'{n} {source} names and addresses not in people.txt (run update-people.py)'
            for source, n in missing.items()]


def distance(a, b):
    """Levenshtein distance."""
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
    return row[-1]


def near(k1, k2, w1, w2):
    """Whether two name keys look like one name: a typo, or spacing."""
    if w1 & w2:
        return True              # same letters in the same order, spaced differently
    t1, t2 = k1.split(), k2.split()
    if len(t1) != len(t2):
        return False
    d1 = [t for t in t1 if t not in t2]
    d2 = [t for t in t2 if t not in t1]
    if len(d1) != 1 or len(d2) != 1:
        return False
    a, b = d1[0], d2[0]
    return min(len(a), len(b)) >= 4 and distance(a, b) <= (1 if min(len(a), len(b)) < 6 else 2)


def review(p, observations):
    uses = collections.Counter()
    recent = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    seen_pairs, seen_bare = set(), set()
    for when, source, name, address, ref in observations:
        pid, _ = p.find(name, address)
        nodes = people.name_nodes(name)
        node = nodes[0] if nodes else None
        if address:
            seen_pairs.add((node, address))
        elif node:
            seen_bare.add(node)
        if pid:
            uses[pid] += 1
            if source != 'tag' and people.name_key(name):
                recent[pid][when.year][name] += 1

    keys, squashed = {}, {}
    by_token = collections.defaultdict(set)
    for pid, person in p.persons.items():
        if person.kind == 'none':
            continue
        keys[pid] = {n[1] for name, _ in person.identities for n in people.name_nodes(name) if n[0] == 'n'}
        squashed[pid] = {re.sub(r'\W|\d|_', '', people.fold(name)) for name, _ in person.identities if name} - {''}
        for k in keys[pid]:
            for t in k.split():
                if len(t) >= 3:
                    by_token[t].add(pid)
    pairs = set()
    for members in by_token.values():
        if len(members) > 300:
            continue
        members = sorted(members)
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                pa, pb = p.persons[a], p.persons[b]
                if (a, b) not in pairs and not keys[a] & keys[b] and not pa.is_not(pb) \
                        and not pb.is_not(pa) and any(
                        near(k1, k2, squashed[a], squashed[b]) for k1 in keys[a] for k2 in keys[b]):
                    pairs.add((a, b))
    out = [f'{len(pairs)} near-matches:']
    out += [f'   {a!r} ({uses[a]})  ~  {b!r} ({uses[b]})'
            for a, b in sorted(pairs, key=lambda ab: (min(uses[ab[0]], uses[ab[1]]), ab))]

    def apart(ids):
        return all(p.persons[a].is_not(p.persons[b]) or p.persons[b].is_not(p.persons[a])
                   for i, a in enumerate(ids) for b in ids[i + 1:])
    shared = [(node, sorted(ids)) for node, ids in p.by_name.items()
              if node[0] == 'n' and len(ids) > 1 and any(p.persons[i].kind != 'none' for i in ids)
              and not apart(sorted(ids))]
    out.append(f'{len(shared)} names that several people have:')
    out += [f'   {node[1]!r}: ' + ', '.join(f'{i!r} ({uses[i]})' for i in ids) for node, ids in sorted(shared)]

    drift = []
    for pid, years in recent.items():
        person = p.persons[pid]
        last = sorted(years)[-2:]
        written = sum((years[y] for y in last), collections.Counter())
        name, count = written.most_common(1)[0]
        same = people.name_key(name) == people.name_key(person.name) or (
            people.OTHER_LETTER.search(name) and not people.LATIN.search(name)
            and people.LATIN.search(person.name))      # the Latin spelling is preferred
        if not same and count >= 5 and count > sum(written.values()) / 2:
            drift.append(f'   {pid!r} has written {name!r} {count} times in {"-".join(map(str, last))}')
    out.append(f'{len(drift)} people writing their name differently from Canonical lately:')
    out += sorted(drift)

    stale = []
    for pid, person in p.persons.items():
        for name, address in person.identities[1:]:
            nodes = people.name_nodes(name)
            node = nodes[0] if nodes else None
            if (address and (node, address) not in seen_pairs) or (not address and node not in seen_bare):
                stale.append(f'   {person.where}: {pid!r}: {people.identity_text(name, address)!r}')
    out.append(f'{len(stale)} Alternate lines that match nothing in the data:')
    out += stale
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().split('\n')[0])
    parser.add_argument('--review', action='store_true',
                        help='also list things to check by hand')
    args = parser.parse_args()
    try:
        data = commitdata.load()
    except commitdata.LoadError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    p = people.load()
    observations = list(people.observations(data=data))

    problems = (p.problems + check_observations(p, observations)
                + check_tags(data, p) + check_duplicates(data, p))
    for problem in problems:
        print(problem, file=sys.stderr)
    if args.review:
        for line in review(p, observations):
            print(line, file=sys.stderr)
    npeople = sum(1 for x in p.persons.values() if x.kind != 'none')
    print(f'{npeople} people, {len(p.persons) - npeople} not people, '
          f'{len(problems)} problems', file=sys.stderr)
    sys.exit(1 if problems else 0)


if __name__ == '__main__':
    main()
