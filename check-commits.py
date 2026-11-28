#!/usr/bin/env python3
"""
Load the commit data and overrides, and report anything that looks wrong.

For tag fixes: an unknown commit or verb, a fix in the wrong year's file
or out of commit order, a delete that matches nothing, an insert or append
of a value that's already there (most likely the extraction now reads
that line itself), and replace mixed with other verbs for one tag.

For line adjustment overrides: an unknown commit or kind, a row in the
wrong year's file or out of commit order, a second row for one commit,
details that don't belong to the kind, a residual larger than the
insertions, a reverted-by commit that isn't later or a revert-date that
isn't its date, a row identical to the generated one, and a normal row
that cancels nothing.

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
        fixes_by_key[fix.hash, commitdata.tag_key(fix.tag)].append(fix)

    for (h, key), fixes in fixes_by_key.items():
        existing = [v for t, v in data.commits[h].raw_tags if commitdata.tag_key(t) == key]
        deleted = {f.value for f in fixes if f.verb == 'delete'}
        for fix in fixes:
            if fix.verb == 'delete' and fix.value not in existing:
                problems.append(f'{fix.where}: {fix.hash} has no {fix.tag} '
                                f'{fix.value!r} to delete')
            if fix.verb in ('insert', 'append') and fix.value in existing \
                    and fix.value not in deleted:
                problems.append(f'{fix.where}: {fix.hash} already has {fix.tag} '
                                f'{fix.value!r}')
        if any(f.verb == 'replace' for f in fixes) and \
                any(f.verb != 'replace' for f in fixes):
            problems.append(f'{fixes[0].where}: replace must be the only verb '
                            f'for {h} {fixes[0].tag}')
    return problems


def check_line_adjustments(data):
    problems = []
    order = {h: i for i, h in enumerate(data.commits)}
    generated = {}
    for a in data.line_adjustments:
        if not a.override:
            generated.setdefault(a.hash, a)
            if a.hash not in data.commits or a.kind not in commitdata.LINE_ADJUSTMENT_KINDS:
                problems.append(f'{a.where}: bad generated row for {a.hash}')
    reverting = {a.details.get('reverted-by') for a in data.line_adjustments
                 if not a.override and a.kind == 'revert'}

    seen, previous = set(), None
    for a in (a for a in data.line_adjustments if a.override):
        commit = data.commits.get(a.hash)
        if commit is None:
            problems.append(f'{a.where}: unknown commit {a.hash}')
            continue
        if commit.year != a.year:
            problems.append(f'{a.where}: {a.hash} is a {commit.year} commit, not {a.year}')
        if previous and order[a.hash] < order[previous]:
            problems.append(f'{a.where}: {a.hash} is older than {previous}; '
                            f'adjustments must be in commit order')
        previous = a.hash
        if a.hash in seen:
            problems.append(f'{a.where}: second override for {a.hash}')
        seen.add(a.hash)
        if a.kind not in commitdata.LINE_ADJUSTMENT_KINDS:
            problems.append(f'{a.where}: unknown kind {a.kind!r}')
            continue

        allowed = {'normal': set(), 'mechanical': {'residual'},
                   'revert': {'reverted-by', 'revert-date', 'residual'}}[a.kind]
        for key in sorted(set(a.details) - allowed):
            problems.append(f'{a.where}: {key}= does not belong on a {a.kind} row')
        if 'residual' in a.details:
            r = a.details['residual']
            if not r.isdigit():
                problems.append(f'{a.where}: residual {r!r} is not a number')
            elif int(r) > commit.insertions:
                problems.append(f'{a.where}: residual {r} is more than the '
                                f'{commit.insertions} lines inserted')
        if a.kind == 'revert':
            by = a.details.get('reverted-by')
            if not by:
                problems.append(f'{a.where}: revert row without reverted-by=')
            elif by not in data.commits:
                problems.append(f'{a.where}: unknown reverting commit {by}')
            elif order[by] <= order[a.hash]:
                problems.append(f'{a.where}: {by} is not later than {a.hash}')
            elif 'revert-date' in a.details and a.details['revert-date'] != str(data.commits[by].utc_date):
                problems.append(f'{a.where}: revert-date {a.details["revert-date"]} is not '
                                f'{by}\'s date, {data.commits[by].utc_date}')

        g = generated.get(a.hash)
        same = {k: v for k, v in (g.details if g else {}).items() if k != 'rule'}
        if g and g.kind == a.kind and same == a.details:
            problems.append(f'{a.where}: same as the generated row ({g.where})')
        if a.kind == 'normal' and not g and a.hash not in reverting:
            problems.append(f'{a.where}: {a.hash} has no generated row to cancel')
    return problems


def main():
    try:
        data = commitdata.load()
    except commitdata.LoadError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    problems = check_tag_fixes(data) + check_line_adjustments(data)
    for problem in problems:
        print(problem, file=sys.stderr)

    ntags = sum(len(c.tags) for c in data.commits.values())
    noverrides = sum(1 for a in data.line_adjustments if a.override)
    print(f'{len(data.commits)} commits, {ntags} tags, '
          f'{len(data.tag_fixes)} tag fixes, '
          f'{noverrides} line-adjustment overrides, {len(problems)} problems',
          file=sys.stderr)
    sys.exit(1 if problems else 0)


if __name__ == '__main__':
    main()
