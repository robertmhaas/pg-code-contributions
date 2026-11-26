#!/bin/sh
# Refresh the split blocks in overrides/tag-fixes/ in place.
#
# usage: tools/legacy-import/run.sh PATH-TO-POSTGRES-CLONE
#
# The clone is only read, for commit messages.  See README.md here.

set -e
pgsql=${1:?usage: run.sh PATH-TO-POSTGRES-CLONE}
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)

python3 -B "$here/split_lists.py" "$root" "$pgsql"
python3 -B "$root/check-commits.py"
