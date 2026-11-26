#!/usr/bin/env python3
"""
Load the commit data and overrides, and report anything that looks wrong.

Exits with status 1 if there are problems.
"""

import collections
import sys

import commitdata


def check_tag_fixes(data):
    problems = []
    fixes_by_key = collections.defaultdict(list)
    order = {h: i for i, h in enumerate(data.commits)}
    previous = None
    for fix in data.tag_fixes:
        commit = data.commits.get(fix.hash)
        if commit is None:
            problems.append(f'{fix.where}: unknown commit {fix.hash}')
            continue
        if commit.year != fix.year:
            problems.append(f'{fix.where}: {fix.hash} is a {commit.year} '
                            f'commit, not {fix.year}')
        if previous and order[fix.hash] < order[previous.hash]:
            problems.append(f'{fix.where}: {fix.hash} is older than '
                            f'{previous.hash}; fixes must be in commit order')
        previous = fix
        if fix.verb not in commitdata.TAG_FIX_VERBS:
            problems.append(f'{fix.where}: unknown verb {fix.verb!r}')
            continue
        fixes_by_key[fix.hash, fix.tag].append(fix)
        existing = [v for t, v in commit.raw_tags if t == fix.tag]
        if fix.verb == 'delete' and fix.value not in existing:
            problems.append(f'{fix.where}: {fix.hash} has no {fix.tag} '
                            f'{fix.value!r} to delete')
        if fix.verb in ('insert', 'append') and fix.value in existing:
            problems.append(f'{fix.where}: {fix.hash} already has {fix.tag} '
                            f'{fix.value!r}')

    for (h, tag), fixes in fixes_by_key.items():
        if any(f.verb == 'replace' for f in fixes) and \
                any(f.verb != 'replace' for f in fixes):
            problems.append(f'{fixes[0].where}: replace must be the only verb '
                            f'for {h} {tag}')
    return problems


def main():
    try:
        data = commitdata.load()
    except commitdata.LoadError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    problems = check_tag_fixes(data)
    for problem in problems:
        print(problem, file=sys.stderr)

    ntags = sum(len(c.tags) for c in data.commits.values())
    print(f'{len(data.commits)} commits, {ntags} tags, '
          f'{len(data.tag_fixes)} tag fixes, {len(problems)} problems',
          file=sys.stderr)
    sys.exit(1 if problems else 0)


if __name__ == '__main__':
    main()
