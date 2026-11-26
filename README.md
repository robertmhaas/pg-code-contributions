# PostgreSQL code contribution data

Data about PostgreSQL code and development contributions, stored as flat
files.

## Conventions

- Everything under `data/` is written by scripts. Don't edit it by hand;
  fix the script and regenerate.
- `overrides/` holds hand-written corrections. Lines starting with `#` are
  comments, used to explain the rows that follow.
- Files use PostgreSQL's COPY text format: tab separated, backslash
  escapes, UTF-8, with a header row. Row order is meaningful, so the files
  aren't meant to be loaded into a database as they are.
- Values are recorded as found, not cleaned up. Normalizing names and
  deciding what counts belong downstream.
- `commitdata.py` reads `data/` and applies `overrides/`, giving other
  scripts the corrected data in memory. `check-commits.py` loads it and
  reports anything that looks wrong.

## Commit metadata

Metadata for every commit on PostgreSQL's `master` branch, extracted by
`extract-commits.py` and refreshed daily.

### Files

One file per year of each kind:

`data/commits/YYYY.tsv`, one row per commit:

| column         | source                                                    |
|----------------|-----------------------------------------------------------|
| `hash`         | `%h`, abbreviated to 11 digits (longer only if ambiguous)  |
| `author`       | `%an`, the git author                                     |
| `committed_at` | `%ci`, the committer date as recorded, with its offset    |
| `insertions`   | insertions from `git log --shortstat -w -M`; 0 if none    |
| `subject`      | `%s`                                                      |

`data/tags/YYYY.tsv`, one row per structured tag in a commit message, so a
commit has as many rows as it has tags:

| column  | meaning                                              |
|---------|------------------------------------------------------|
| `hash`  | as above                                             |
| `tag`   | the key as typed, e.g. `Reviewed-by` or `Reviewed-By`  |
| `value` | the rest of the line as typed, e.g. `Jane Doe <jd@example.com>` |

Rows are in `master` order, oldest first, and tags are in the order they
appear in the message.

### Rules

**Commits.** Every commit reachable from `master`. History there is linear
(no merges), so a back-patch counts once, on `master`.

**Insertions.** `-w` ignores whitespace-only changes, so a pgindent run
counts only the lines it actually changed, and `-M` makes a moved file
count as a rename rather than a fresh copy. A commit that only changes
whitespace has no stat line and gets 0.

**Years.** A commit belongs to the UTC year of its committer date. One
exception: a commit is never dated earlier than the commit before it on
`master`, because `master` only moves forward. That guards against skewed
clocks and author dates carried over as committer dates. (So far only 29
commits have gone backwards, all by less than a day, and none of those
changed year.) Using UTC means a few commits sit in a file whose year
differs from the date written in `committed_at`. For example
`2019-01-01 08:53:02 +0900` is 2018-12-31 in UTC, so it's in 2018.

**Tags.** A tag is any line of the form `Key: value`, anywhere in the
message except the first line. The key is either

- a word or hyphenated words: `Author`, `Reviewed-by`, `Backpatch-through`, or
- for older commits, a phrase of up to four words ending in `by`:
  `Submitted by`, `Reviewed by`, `Pointed out by`.

Spaces are allowed before the colon, and at least one space or tab is
required after it, which rules out URLs and times. An indented line right
after a tag continues its value. This is useful when a tag's values sit on
the following lines, as in `Discussion:` followed by a list of URLs. A
tag with no value is dropped.

Some older commits have no blank line after the first line. Their tags are
still found, but their subject (`%s`) then runs on into the rest of that
first paragraph.

The trailer convention came in gradually, so older commits are messier.
Prose such as `Patch by Jane Doe, reviewed by John Roe` isn't a tag and isn't
captured. Lines that happen to look like tags are captured, for example
`ERROR: ...` pasted from psql or `Note: ...` in a paragraph. Use the tag
name to pick the ones you want.

### Tag fixes

Some commit messages are malformed, e.g. `Author:Jane Doe` with no space,
or `Author: Author: Jane Doe`, and older ones give credit only in prose
("Patch by Jane Doe, reviewed by John Roe"). Rather than bend the
extraction rules to fit, `data/tags/` keeps what was parsed, and
`overrides/tag-fixes/YYYY.tsv` records the corrections, one file per year
like `data/`. Rows are in commit order, each commit's rows preceded by a
comment saying why:

| column  | meaning                                          |
|---------|--------------------------------------------------|
| `hash`  | the commit, as in `data/commits/`                |
| `verb`  | `insert`, `append`, `delete` or `replace`        |
| `tag`   | the tag, matched exactly as typed                |
| `value` | the value to add or delete                       |

Each tag of a commit is fixed separately:

- `insert` and `append` add a value before the tag's existing values or
  after them, in file order. If the commit has no values for that tag,
  they go before or after all of its tags.
- `delete` removes every value that matches exactly.
- `replace` discards all the tag's existing values and puts the replace
  rows, in file order, where the first one was. It must be the only verb
  used for that commit and tag.

So for a tag with values `a b c`, `insert d`, `insert e`, `append f`,
`delete b` gives `d e a c f`.

Where the prose allows, fixes put tags in a canonical form: one person per
tag, with a parenthetical gloss saying what they did. "Bob Jones, with a
test case from Fred Smith" becomes `Author: Bob Jones` and
`Co-authored-by: Fred Smith (test case)`, and "me" becomes the committer.

`commitdata.py` applies fixes even when they're stale or contradictory,
so tools keep working. `check-commits.py` reports the problems:

- an unknown commit or verb,
- a fix in the wrong year's file, or out of commit order,
- a `delete` that matches nothing,
- an `insert` or `append` of a value that's already there (most likely
  the extraction now reads that line itself),
- `replace` mixed with other verbs.

### Line adjustments

A commit's `insertions` overstate the effort behind it when the commit was
later reverted, or when it's mechanical: translation updates, imported or
generated files, pgindent runs, code moved from one file to another. Line
adjustments say which commits those are. A commit's credited lines are its
insertions after the adjustments.

`data/line-adjustments/YYYY.tsv` is written by `classify-commits.py`:

| column    | meaning                                                    |
|-----------|------------------------------------------------------------|
| `hash`    | the commit                                                 |
| `kind`    | `revert` or `mechanical`                                   |
| `details` | comma-separated `key=value` pairs, below                   |

- `reverted-by=HASH`: the commit that reverts it.
- `revert-date=YYYY-MM-DD`: that commit's date, in UTC like the year
  files, so reverts can be counted in different ways, such as only within
  the commit's own year.
- `residual=N`: only part of the commit is discounted; `N` lines are still
  credited. Absent when the whole commit is discounted.
- `rule=NAME`: why the row was generated.

The rules:

- **Reverts**, at any size. A commit whose subject starts with "Revert"
  or whose message says "This reverts commit" reverts:
  - the commits it names by hash in a sentence about reverting
    (`rule=revert-message`);
  - the commits in a list of hashes, as in "The following commits are
    reverted:" (`revert-list`);
  - the commit whose subject it quotes, as in `Revert "..."`
    (`revert-subject`);
  - if it says it also reverts follow-up commits, the commits in between
    that mention one of those by hash (`revert-follow-up`).

  A revert counts as reverting another revert only if it says so plainly
  ("This reverts commit X" or a quoted subject), not just because it
  mentions one. Reverting a revert puts back what that revert reverted,
  so those commits are credited again, and aren't treated as reverted a
  second time even if the message names them. A revert that deletes less than 30% of what its targets
  inserted (for targets of 100 or more lines) only reverted part of them,
  so they aren't treated as reverted.
- **Translation updates**, at any size: a commit that touches only
  translation files (`.po` files and the `nls.mk` that lists them) is
  mechanical (`translation`).
- **Mechanical**, only for commits of 300 or more lines. If the subject
  says it's pgindent, perltidy, a copyright update, a translation update,
  or reformatting, the whole commit is mechanical (`format-subject`).
  Otherwise each file is judged on its own:
  - a generated or imported file, such as a `.po` file, a Unicode table,
    time zone data, Snowball, `config.guess`, or autoconf's `configure`, is
    all mechanical
    (`generated-files`);
  - a file whose added words are at least 97% rearranged deleted words is
    all mechanical (`reformat`): a reformat, or a bulk edit such as adding
    a column to every entry of a catalog `.dat` file;
  - in any other file, only the lines that match lines deleted elsewhere
    in the commit are mechanical (`code-motion`).

  If at least half the commit's lines are mechanical, the commit is, and
  the rest of its lines are the residual. `rule=` names the biggest
  share.
  A residual under 50 lines isn't recorded.

The rules are meant to be mostly right, not perfect.
`overrides/line-adjustments/YYYY.tsv` corrects them, with `#` comments like
the tag fixes. Its columns are the same, `details` may be left off, and its
rows are in commit order. `revert-date` may be left off a revert row, but if
it's there it must match the reverting commit. A row there replaces every generated row for its
commit. Kind `normal` cancels them: the commit is credited in full.

Credited lines are then:

- a commit with no adjustment: its insertions;
- a mechanical commit: its residual, or 0;
- a reverted commit: its residual, or 0, unless the revert was itself
  reverted;
- a commit that reverts others: 0, unless it has a row of its own.

`check-commits.py` also reports overrides for unknown commits, unknown
kinds, residuals larger than the insertions, `reverted-by` commits that
aren't later, overrides identical to the generated row, and `normal` rows
that cancel nothing.

### Using the data

```python
import commitdata
data = commitdata.load()
commit = data.commits['a8eeb22f171']
commit.raw_tags         # [(tag, value)] as extracted
commit.tags             # with tag fixes applied
commit.authors          # see below
commit.primary_author   # authors[0], who is credited with the lines
commit.credited_lines   # insertions after line adjustments
commit.adjustment       # the line adjustment in effect, if any
commit.reverted_by      # the revert in effect, if any
commit.reverted_at      # its date

# credit that a later year's revert doesn't take away, as in the
# yearly posts
data.credited_lines(commit, reverts_until=datetime.date(commit.year, 12, 31))
```

A commit's authors are all and only the people in its `Author` and
`Co-authored-by` tags, after tag fixes, in the order listed; the git author
is then disregarded. Only if there are no such tags is the commit credited
to its git author. So a committer who wrote part of a patch has to list
themselves. The first author listed is the primary author, who is credited
with the commit's lines. Values are as written, emails and glosses
included; mapping them to people is a later step.

`commit.year` is the year of the file the commit came from. Run
`python3 check-commits.py` after changing anything under `overrides/`.

### Updating

The `Extract commits` Action runs daily at 08:23 UTC. It also runs when
`extract-commits.py` changes, and you can start it by hand. Each run clones
PostgreSQL from the GitHub mirror, runs `extract-commits.py` and then
`classify-commits.py`, and commits `data/` if anything changed.
`classify-commits.py` classifies only the commits added since its last
run, recorded in `data/classify-state.tsv`, and updates any older commits
that those reverted. It reclassifies everything when `classify-commits.py`
itself changes, or with `--all`.

A run rewrites only the years whose list of commits has changed, which in
practice is the current year. The previous year can change too, around New
Year. A run triggered by a change to `extract-commits.py`, or a manual run with
"all" ticked, rewrites every year.

To run it locally, against an up-to-date clone (a stale one rewrites the
current year without the commits it's missing):

```sh
python3 extract-commits.py --repo ~/pgsql --ref origin/master          # changed years
python3 extract-commits.py --repo ~/pgsql --ref origin/master --all    # everything
```

A full rewrite takes about 40 seconds on an 8-core laptop, with years
processed in parallel.
