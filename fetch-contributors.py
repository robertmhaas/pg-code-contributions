#!/usr/bin/env python3
"""
Write data/recognized-contributors.tsv from the Recognized Contributors page.

One row per person on https://www.postgresql.org/community/contributors/,
in page order: the section heading, the username from their
/community/people/ link, their name, and their address as the page shows
it ("andres at anarazel.de").  Username and address are empty when the
page has none.  The file is the page's current state, not a history.
"""

import html.parser
import os
import sys
import urllib.request

import commitdata

URL = 'https://www.postgresql.org/community/contributors/'
OUTPUT = os.path.join('data', 'recognized-contributors.tsv')
USER_AGENT = 'pg-code-contributions (https://github.com/robertmhaas/pg-code-contributions)'
PEOPLE = '/community/people/'


class ContributorParser(html.parser.HTMLParser):
    """Collects [section, username, name, address] rows.

    Each contributor-table follows an <h2> naming its section.  A table
    with column headings has a person in the first column and what they
    did in the second; one without (Past Contributors) has a person in
    every cell.  A person's cell starts with "Name" or "Name (address)",
    the name possibly linked to /community/people/<username>/.
    """

    def __init__(self):
        super().__init__()
        self.rows = []
        self.section = None
        self.in_h2 = False
        self.in_table = False
        self.headed = False     # this table has <th> column headings
        self.column = 0         # index of the next <td> in this <tr>
        self.cell = None        # [username, text] inside a person's <td>

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'h2':
            self.in_h2 = True
            self.section = ''
        elif tag == 'table':
            self.in_table = 'contributor-table' in (attrs.get('class') or '').split()
            self.headed = False
        elif not self.in_table:
            pass
        elif tag == 'th':
            self.headed = True
        elif tag == 'tr':
            self.column = 0
        elif tag == 'td':
            if self.column == 0 or not self.headed:
                self.cell = ['', '']
            self.column += 1
        elif self.cell is None:
            pass
        elif tag == 'a' and not self.cell[1].strip():
            href = attrs.get('href') or ''
            if href.startswith(PEOPLE):
                self.cell[0] = href[len(PEOPLE):].strip('/')
        elif tag == 'br':
            self.cell[1] += '\n'

    def handle_endtag(self, tag):
        if tag == 'h2':
            self.in_h2 = False
            self.section = ' '.join(self.section.split())
        elif tag == 'table':
            self.in_table = False
        elif tag == 'td' and self.cell is not None:
            username, text = self.cell
            self.cell = None
            line = text.strip().split('\n')[0].strip()
            name, address = line, ''
            if line.endswith(')') and ' (' in line:
                name, address = line[:-1].split(' (', 1)
            self.rows.append([self.section, username, ' '.join(name.split()),
                              address.strip()])

    def handle_data(self, data):
        if self.in_h2:
            self.section += data
        elif self.cell is not None:
            self.cell[1] += data


def escape(value):
    return (value.replace('\\', '\\\\').replace('\t', '\\t')
            .replace('\n', '\\n').replace('\r', '\\r'))


def main():
    request = urllib.request.Request(URL, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        page = response.read().decode('utf-8')

    parser = ContributorParser()
    parser.feed(page)
    parser.close()
    rows = parser.rows
    # Refuse to replace the file with a short or garbled one if the page
    # changes shape or something else is served in its place.
    sections = {r[0] for r in rows}
    if 'Core Team' not in sections or len(rows) < 100 or not all(r[2] for r in rows):
        sys.exit(f'found {len(rows)} contributors in {len(sections)} sections; '
                 'page format may have changed')

    path = os.path.join(commitdata.ROOT, OUTPUT)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('section\tusername\tname\taddress\n')
        for row in rows:
            f.write('\t'.join(escape(v) for v in row) + '\n')
    print(f'{len(rows)} contributors in {len(sections)} sections')


if __name__ == '__main__':
    main()
