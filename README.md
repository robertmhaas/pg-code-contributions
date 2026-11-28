# PostgreSQL code contribution data

Data about who contributes to PostgreSQL: every commit on `master`, with
the credits in its message, and every message posted to pgsql-hackers.
It's what the yearly contribution posts are computed from.

The data is extracted by scripts and then corrected by hand. If you find
something wrong about yourself or anyone else, see
[Corrections](#corrections) for what to put in a pull request.

## How contributions are counted

### Commits and lines

- Every commit reachable from `master` counts. History there is linear,
  so a back-patch counts once, on `master`.
- A commit belongs to the UTC year of its committer date. A commit is
  never dated earlier than the commit before it on `master`, because
  `master` only moves forward.
- A commit's size is the lines it inserts, as counted by
  `git log --shortstat -w -M`. `-w` ignores whitespace-only changes, so
  reindenting code doesn't count and a pgindent run counts only the lines
  it actually changed. `-M` detects renames, so a moved file doesn't count
  as new code. Deleted lines count for nothing.

Some commits insert many more lines than the work behind them, so their
lines are adjusted:

- A **reverted** commit is credited with no lines, and neither is the
  revert. If the revert is itself reverted, the original is credited
  again. The yearly posts count a revert only if it happened in the same
  year as the commit.
- A **mechanical** commit is credited only with the lines that aren't
  mechanical. Mechanical lines are pgindent runs and similar reformatting,
  copyright updates, imported or generated files, and code moved from
  one file to another. This applies only to commits of 300 lines or more
  whose lines are at least half mechanical. Translation updates are
  mechanical at any size.

The rules that find these commits are in `classify-commits.py`. They're
meant to be mostly right, and the ones they get wrong are corrected by
hand.

### Authors and other credits

A commit's authors are the people in its `Author` and `Co-authored-by`
tags, in the order listed. If it has either tag, those people are its
only authors, and the git author doesn't count, so a committer who wrote
part of a patch has to list themselves. Only a commit with neither tag is
credited to its git author.

The **first author listed is the primary author**, and is credited with
all of the commit's lines.

Other people are credited by role, with no lines: `Reviewed-by`,
`Reported-by`, `Tested-by`, `Suggested-by` and `Diagnosed-by`.

Many older commit messages give credit only in prose, as in "Patch by
Jane Doe, reviewed by John Roe", and some tags are malformed. Tag fixes
(see below) turn these into the tags above. That has been done for
commits since 2011.

Committers are also counted for the lines of other people's patches they
committed. A commit's committer is its git author, which PostgreSQL's
committers set to themselves.

### Mail

Each message posted to pgsql-hackers counts once, for its sender, in the
year of the archive file it's in. The rules for reading the archive are in
`extract-mail.py`.

### People

Names and addresses in commits and mail are mapped to people through
`people.txt`. An address belongs to one person. A name usually does, and
names are compared ignoring case, accents, punctuation and word order. A
name that several people share identifies none of them on its own. So
does a bare first name. The lookup rules are in `people.py`.

Tools such as Coverity are marked as not being people, and aren't
credited as authors.

## Corrections

Nothing under `data/` is edited by hand. Scripts regenerate it, and the
corrections are kept separately:

| what's wrong                                               | where to fix it                     |
|------------------------------------------------------------|-------------------------------------|
| a commit's credits: someone missing, in the wrong role, the authors in the wrong order, a misspelled name | `overrides/tag-fixes/YYYY.tsv`      |
| a commit's lines: a revert or mechanical change missed, or one found wrongly | `overrides/line-adjustments/YYYY.tsv` |
| a person split in two, two people merged, the wrong name shown | `people.txt`                        |

In a pull request:

- Put a `#` comment above your rows saying why, quoting the commit message
  or linking the discussion. That's the evidence a reviewer will check.
- Run `python3 check-commits.py` and `python3 check-people.py`, and fix
  anything they report.

The override files use PostgreSQL's COPY text format: tab-separated, with
a header row and `#` comments. `YYYY` is the commit's year, which is the
`data/commits/` file it's in, and rows go in commit order, oldest first.
Hashes are written as in `data/commits/`.

### Tag fixes

`overrides/tag-fixes/YYYY.tsv` changes the tags parsed from commit
messages (`data/tags/`). Its columns are `hash`, `verb`, `tag` and
`value`. For example:

```
# "Dagfinn Ilmari Mannsåker, reviewed by Shubham Khanna. Some subtractions by me."
0d9937d1185	insert	Author	Dagfinn Ilmari Mannsåker
0d9937d1185	append	Reviewed-by	Shubham Khanna
0d9937d1185	append	Co-authored-by	Robert Haas (subtractions)
```

Each tag of a commit is fixed separately. Tags are matched ignoring case
and spacing, so a fix to `Reviewed-by` also applies to `Reviewed-By` and
`Reviewed by`.

- `insert` adds a value before the tag's existing values, and `append`
  adds one after them, in file order. If the commit has no values for that
  tag, they go before or after all of its tags.
- `delete` removes every value that matches exactly.
- `replace` discards all of the tag's values and puts the `replace` rows
  where the first value was. Don't use it with other verbs on the same tag.

So for a tag with values `a b c`, the fixes `insert d`, `insert e`,
`append f` and `delete b` give `d e a c f`. To make someone the primary
author, put them first.

Fixes put credits in a standard form:

- **One person per tag**, with a gloss in parentheses where the message
  says what they did: `Co-authored-by: Fred Smith (test case)`.
- **Standard keys**: `Author`, `Co-authored-by`, `Reviewed-by`,
  `Reported-by`, `Tested-by`, `Suggested-by` and `Diagnosed-by`. A synonym
  or misspelling is replaced, so `Spotted-by` becomes `Reported-by` and
  `Reivewed-by` becomes `Reviewed-by`.
- **Full names**: "me", or the committer's first name, becomes the
  committer's name. A first name spelled out elsewhere in the message
  becomes the full name.

Fixes follow what the message says:

- Writing or editing part of the patch, including "kibitzing" and
  rewriting comments, makes someone a co-author. So does "based on an
  earlier patch by X".
- Reviewing or testing a patch never makes someone an author, and
  neither do suggestions about its wording or comments, which are review.
- Proposing a fix that the patch implements is `Suggested-by`, not
  authorship.
- "Reported", "noted", "pointed out" and "per X" are `Reported-by`.
- A credit to an AI tool goes to the person or organization that used
  it, with the tool in a gloss: `Jane Doe (using Claude)`.
- If the message credits no one as author, no `Author` tag is added, and
  the git author gets the credit.

### Line adjustments

`overrides/line-adjustments/YYYY.tsv` corrects the adjustments that
`classify-commits.py` writes to `data/line-adjustments/`. Its columns are
`hash`, `kind` and `details`. For example:

```
# Markup conversion: no more SGML empty tags.
c29c578908d	mechanical

# Reverted in "Revert 56-bit relfilenode change and follow-up commits."
05d4cbf9b6b	revert	reverted-by=a448e49bcbe,revert-date=2022-09-28
```

A row replaces every generated row for its commit. `kind` is one of:

- `mechanical`: the commit's lines aren't credited, except
  `residual=N` lines if `details` gives that.
- `revert`: the commit was reverted. `details` has
  `reverted-by=HASH`, the reverting commit, and may have `residual=N` and
  `revert-date=YYYY-MM-DD`, which must be the reverting commit's UTC date.
- `normal`: the commit is credited in full. Use this for a commit the
  rules wrongly found to be mechanical or reverted, or for a revert that
  should be credited for lines of its own.

### people.txt

`people.txt` has one stanza per person, separated by blank lines:

```
Canonical: Robert Haas <robertmhaas(at)gmail(dot)com>
Alternate: Robert Hass <robertmhaas(at)gmail(dot)com>
Alternate: Robert Haas <rhaas(at)postgresql(dot)org>
```

- `Canonical:` is the name and address to show the person by. It also
  identifies them in reports, so it must be unique.
- `Alternate:` is another name and address the person has used, or a
  name or address on its own.
- `Distinct:` names someone with a similar name who is a different
  person, so `check-people.py --review` stops suggesting them as a merge.
- `Kind: none` marks something that isn't a person, such as a tool or a
  stray word.

Addresses are written as in `data/mail/` (`(at)` and `(dot)`), or
plainly.

`update-people.py` adds new names and addresses from new commits and
mail, but it never merges, moves or removes anything. To merge two
stanzas, move one's lines into the other. To split one, move lines out.
To change how someone is shown, edit `Canonical:`.

## Files and scripts

- `data/` is written by scripts. Its files are in PostgreSQL's COPY text
  format, one per year, with a header row. Row order is meaningful, so the
  files aren't meant to be loaded into a database as they are.
- `overrides/` and `people.txt` are edited by hand, as above.
  `update-people.py` also adds to `people.txt`.

| script                | what it does                                              |
|-----------------------|-----------------------------------------------------------|
| `extract-commits.py`  | writes `data/commits/` and `data/tags/` from a PostgreSQL clone |
| `classify-commits.py` | writes `data/line-adjustments/`: reverted and mechanical commits |
| `extract-mail.py`     | writes `data/mail/` from the archive's mbox files         |
| `update-people.py`    | adds new names and addresses to `people.txt`              |
| `commitdata.py`       | loads the commit data with the overrides applied          |
| `people.py`           | loads `people.txt` and maps names and addresses to people |
| `check-commits.py`    | reports problems with the commit data and overrides       |
| `check-people.py`     | reports problems with `people.txt`; `--review` lists likely merges |
| `export-db.py`        | writes everything as a PostgreSQL dump, with report views |
| `export-json.py`      | writes the yearly figures as JSON                         |

Each script's docstring gives its rules, its output format and how to run
it.

A GitHub Action (`.github/workflows/extract-commits.yml`) keeps the commit
data current. It runs `extract-commits.py`, `classify-commits.py` and
`update-people.py`, and commits the result. Mail isn't automated, because
downloading the archive needs a postgresql.org community account. After
`extract-mail.py`, run `update-people.py` by hand.

To run reports:

```sh
python3 export-db.py -o contributions.sql
createdb contributions
psql -q -f contributions.sql contributions
```

```sql
SELECT "#", author, lines, pct_lines, cum_pct_lines, commits
  FROM top_authors WHERE year = 2025 ORDER BY "#" LIMIT 30;
```
