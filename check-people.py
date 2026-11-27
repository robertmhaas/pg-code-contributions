#!/usr/bin/env python3
"""
Load the people layer and report anything that looks wrong.

Reports pins that don't parse or name an identifier that never occurs,
and identifiers pinned to two people.  For commits since 2011, where the
tag fixes start, also reports person tags that list several people or
name someone who can't be identified, credit keys that aren't one of
people.PERSON_TAGS, and people named twice in one tag.  With --review, also lists pairs of people whose
names are near-matches, such as "Masahiko Sawada" and "Masahiko Sawasa",
for checking by hand; pin a pair to one person to merge them, or each to
its own to stop the suggestion.

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


def check_pins(p):
    problems = list(p.problems)
    for pin in p.pins:
        if pin.node not in p.seen:
            problems.append(f'{pin.where}: {pin.identifier!r} doesn\'t occur in the data')
    return problems


def check_tags(data, p):
    """Person tag values that name someone who can't be identified."""
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
            elif (key.endswith('-by') or key.startswith(('author', 'co-author'))) \
                    and key not in NOT_CREDITS:
                bad['not a canonical key', f'{tag}: {value}'].append(c.hash)
    problems = []
    for (status, text), hashes in sorted(bad.items(), key=lambda kv: (kv[0][0], -len(kv[1]))):
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


def review(p):
    """[(person, person)] whose names are near-matches, least-used first."""
    keys, squashed, uses, by_token = {}, {}, {}, collections.defaultdict(set)
    for pid, person in p.persons.items():
        keys[pid] = {n[1] for n in person.nodes if n[0] == 'n'}
        written = person.own_names + person.tag_names
        squashed[pid] = {re.sub(r'\W|\d|_', '', people.fold(n)) for n in written if n}
        squashed[pid].discard('')
        uses[pid] = sum(written.values())
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
                if (a, b) in pairs or (p.persons[a].pinned and p.persons[b].pinned):
                    continue
                if any(near(k1, k2, squashed[a], squashed[b]) for k1 in keys[a] for k2 in keys[b]):
                    pairs.add((a, b))
    return sorted(pairs, key=lambda ab: (min(uses[ab[0]], uses[ab[1]]), ab)), uses


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().split('\n')[0])
    parser.add_argument('--review', action='store_true',
                        help='also list near-matching names to check by hand')
    args = parser.parse_args()
    try:
        data = commitdata.load()
    except commitdata.LoadError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    p = people.load(data=data)

    problems = check_pins(p) + check_tags(data, p) + check_duplicates(data, p)
    for problem in problems:
        print(problem, file=sys.stderr)
    if args.review:
        pairs, uses = review(p)
        print(f'{len(pairs)} near-matches:', file=sys.stderr)
        for a, b in pairs:
            print(f'   {a!r} ({uses[a]})  ~  {b!r} ({uses[b]})', file=sys.stderr)
    print(f'{len(p.persons)} people, {len(p.pins)} pins, {len(problems)} problems',
          file=sys.stderr)
    sys.exit(1 if problems else 0)


if __name__ == '__main__':
    main()
