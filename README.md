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

## Mailing list messages

One row per message posted to pgsql-hackers, extracted by `extract-mail.py`
from the list archive's monthly mbox files.

### Files

`data/mail/pgsql-hackers/YYYY.tsv`, one row per message, in archive order:

| column         | source                                                     |
|----------------|------------------------------------------------------------|
| `message_id`   | `Message-ID`, without angle brackets, as in a `Discussion:` link |
| `sent_at`      | `Date`, as `YYYY-MM-DD HH:MM:SS +hhmm` with the sender's offset |
| `from_name`    | the display name in `From`, decoded; empty if there's none |
| `from_address` | the address in `From`                                      |

### Rules

**Messages.** The archive starts each message with a `From ` line whose
envelope sender is a list's owner address: `pgsql-hackers-owner+...`, or
another list's for a cross-posted message. Only those lines start a
message. The archive doesn't escape `From ` in message bodies, so other
lines that look like the start of a message are part of the message
they're in. Most of them are the `From <sha1> Mon Sep 17 00:00:00 2001`
line that begins `git format-patch` output, 700 to 2,300 a year; the
counts behind the 2019–2025 posts took each of those for a message from
the patch's author.

**Years.** A message belongs to the year of the archive file it's in. The
archive files a message under the UTC month of its `Date`, except, it
seems, when that's later than when the message arrived. If a message has
no usable `Date`, `sent_at` is when the archive received it, in UTC.

**Names.** Encoded words (RFC 2047) are decoded wherever they are,
including inside quotes, where many clients put them, and bytes labelled
Latin-1 or another single-byte charset are read as UTF-8 if they're valid
UTF-8. `Name <address>`, `address (Name)` and a bare address are all
understood. Otherwise names are as written: `Zhou, Zhiguo`,
`Hayato Kuroda (Fujitsu)` and an address used as a name are left for
mapping to people later.

**Addresses** are written the way the archive's web pages write them,
`tgl(at)sss(dot)pgh(dot)pa(dot)us`, including any that appear in a name.
Like the archive's own, that only deters trivial scraping.

### Updating

This isn't automated, since downloading the archive's mbox files needs a
postgresql.org community account. Download every month of each year you
want written, from the mbox link on the archive's month pages, then run

```sh
python3 extract-mail.py --mbox-dir DIR
```

It finds `pgsql-hackers.YYYYMM` files anywhere under `DIR` and rewrites
the file for each year it finds, rebuilt from that year's mbox files
alone; `--years` limits which. Ten years take about 20 seconds.

## People

`people.py` maps the names and addresses in commit tags, git authors and
mail senders to people. It computes the mapping when loaded; nothing is
written under `data/`.

### How people are found

A person tag is `Author` or any tag ending in `-by`. Each name and
address in them, in git authors, and in mail senders is an identifier:

- An **address** is compared case-insensitively, with the archive's
  `(at)` and `(dot)` undone.
- A **name** is compared by a key that ignores case, accents, punctuation,
  word order, and initials if two other words are left. So `Zhou, Zhiguo`
  and `zhiguo zhou` match, as do `Álvaro Herrera` and `Alvaro Herrera`,
  and `Andrey M. Borodin` and `Andrey Borodin`. Addresses, quotes, and
  glosses or affiliations in trailing parentheses are removed first. A
  name written in two scripts, like `Hou Zhijie 侯志杰` or
  `王海洋 (Haiyang Wang)`, also counts as each script's part.
- A **one-word name**, like `MauMau` or `Michael`, is too weak to link
  anything, and identifies no one unless it's pinned (below).

A name and an address written together, in a mail `From` or a tag like
`Jane Doe <jd@example.com>`, are the same person, and so is everything
linked to either of them.

A tag value naming several people, like `Jane Doe and John Roe` or
`Jane Doe, John Roe`, is split. When the only separators are commas and
there's an address, it takes two full names to make a list, so
`Wong, Yi Wen <yw@example.com>` is one person.

A person's display name is how they write it themselves, as git author
or in mail, or failing that how commit tags write it, preferring the
Latin alphabet. The most used spelling wins; among spellings differing
only in case or accents, one that isn't all upper or lower case, then
one with accents.

### Overrides

`overrides/people.tsv` has two columns, `person` and `identifier`, with
`#` comments like the other overrides. Each row says the identifier (a
name or an address, written either way) belongs to the person. The
`person` column is also the display name, and if it's a full name it's
pinned too. So:

- Rows with the same person merge everything linked to their identifiers.
  `Atsushi Torikoshi	torikoshia` joins the `torikoshia` mail account
  to every `Atsushi Torikoshi` tag.
- Links never join two different pinned people. An identifier that would
  is ambiguous and identifies no one. Two people with the same name are
  told apart by pinning their addresses to, say, `Jane Doe (Example)` and
  `Jane Doe (Elsewhere)`; a bare `Jane Doe` tag is then ambiguous, and the
  fix goes in the tag fixes.
- Person `-` means the identifier identifies no one: tools such as
  `Coverity`, AI tools such as `Claude`, or the web forms' shared address.
  Their credits stay in the tags, and each use of the data decides what
  to do with them; `authors()` skips them. Organizations that report
  bugs, such as the `Anthropic OSS program`, are counted like people.
- A row naming a person as themselves, `Jane Doe	Jane Doe`, keeps
  them out of `check-people.py --review`'s near-matches.

The mapping doesn't depend on where a value appears, so a name that means
different people in different commits, like `Michael` or `Peter`, is fixed
in the tag fixes, not here.

`python3 check-people.py` reports rows that don't parse or whose
identifier doesn't occur in the data, identifiers pinned to two people,
person tag values that identify no one, and anyone named twice in one
tag of a commit. With `--review` it also lists
pairs of people whose names are near-matches, like `Masahiko Sawada` and
`Masahiko Sawasa`, to merge or keep apart.

### Using it

```python
import people
p = people.load()
p.resolve_value('Jane Doe <jd@example.com> (test case)')
                        # [(text, person id or None, status)], one per person named
p.resolve('Jane Doe', 'jd(at)example(dot)com')   # (person id or None, status)
p.persons['Jane Doe']   # Person: id, pinned, nodes, own_names, tag_names
p.authors(commit)       # commit.authors split into people, without those
                        # that identify no one, such as AI tools
p.primary_author(commit)   # the first of those, credited with the lines
```
