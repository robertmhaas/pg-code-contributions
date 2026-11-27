#!/usr/bin/env python3
"""
Add what commits and mail say about people to people.txt.

Every name and address in the data (git authors, person tags and mail
senders, oldest first) is looked up, and only what's new is added:

- a new name at a known address joins that address's person;
- a new address joins the one person with that name, or else starts a new
  person, even when several people already have the name;
- a full name seen without an address that no one has starts a new
  person, as does one that several people have, unless one of them lists
  it on its own.

One-word names start no one.  Nothing is ever merged, moved or removed,
so a split made by hand stays split; merging is done by editing the file.
"""

import argparse
import os
import sys

import commitdata
import people


def split_blocks(text):
    """(header blocks, stanza blocks) of people.txt, each a list of lines."""
    blocks = [b.split('\n') for b in text.strip('\n').split('\n\n') if b.strip()]
    first = next((i for i, b in enumerate(blocks)
                  if any(l.startswith('Canonical:') for l in b)), len(blocks))
    return blocks[:first], blocks[first:]


def canonical(block):
    return next(l for l in block if l.startswith('Canonical:'))[len('Canonical:'):].strip()


def sort_key(value):
    name, address = people.parse_identity(value)
    return people.fold(name or address or ''), address or ''


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().split('\n')[0])
    parser.add_argument('--dry-run', action='store_true',
                        help="print what would be added, but don't change people.txt")
    args = parser.parse_args()

    root = commitdata.ROOT
    path = os.path.join(root, people.REGISTRY)
    p = people.load(root)
    if p.problems:
        for problem in p.problems:
            print(problem, file=sys.stderr)
        sys.exit('people.txt has problems; fix them first')

    added = {}          # person id: [Alternate values]
    created = []        # Canonical values

    def create(name, address):
        value = people.identity_text(name, address)
        if value in p.persons:
            return      # can only be a name-only person already there
        p.add_person(people.Person(value, name, address, 'person', [(name, address)], 'new'))
        created.append(value)

    def add(pid, name, address):
        p.index(pid, name, address)
        added.setdefault(pid, []).append(people.identity_text(name, address))

    observations = sorted(people.observations(root), key=lambda o: (o[0], o[1], o[4]))
    for when, source, name, address, ref in observations:
        nodes = people.name_nodes(name)
        node = nodes[0] if nodes else None
        full = node is not None and node[0] == 'n'
        if address:
            pid = p.by_address.get(address)
            if pid is None and full and len(p.by_name.get(node, ())) == 1:
                pid = next(iter(p.by_name[node]))
            if pid is None:
                create(name if node else '', address)
            elif node and (node, address) not in p.pairs[pid]:
                add(pid, name, address)
        elif full:
            if p.by_bare_name.get(node) or len(p.by_name.get(node, ())) == 1:
                continue
            create(name, None)

    count = sum(len(v) for v in added.values())
    print(f'{count} names and addresses added to {len(added)} people; '
          f'{len(created)} new people', file=sys.stderr)
    if args.dry_run:
        for pid, values in added.items():
            print(f'{pid}: + ' + '; '.join(values))
        for value in created:
            print(f'new: {value}')
        return
    if not added and not created:
        return

    with open(path, encoding='utf-8') as f:
        header, stanzas = split_blocks(f.read())
    for block in stanzas:     # people already in the file; new ones get theirs below
        block += [f'Alternate: {v}' for v in added.get(canonical(block), [])]
    for value in created:
        key = sort_key(value)
        at = next((i for i, b in enumerate(stanzas) if sort_key(canonical(b)) > key), len(stanzas))
        stanzas.insert(at, [f'Canonical: {value}'] + [f'Alternate: {v}' for v in added.get(value, [])])
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n\n'.join('\n'.join(b) for b in header + stanzas) + '\n')


if __name__ == '__main__':
    main()
