#!/usr/bin/env python3
"""
Extract per-message sender metadata from PostgreSQL mailing list archives.

Reads the archive's monthly mbox files, named LIST.YYYYMM (for example
pgsql-hackers.202503), from anywhere under --mbox-dir, and for every year
writes

    data/mail/LIST/YYYY.tsv   message_id  sent_at  from_name  from_address

in PostgreSQL COPY text format.  See README.md for the rules.

A year's file is rebuilt from all of that year's mbox files found, so give
it every month of the years you want written.
"""

import argparse
import base64
import binascii
import codecs
import collections
import concurrent.futures
import datetime
import email.utils
import os
import re
import sys

COLUMNS = ['message_id', 'sent_at', 'from_name', 'from_address']

# The archive starts every message with a "From " line whose envelope sender
# is a list's owner address: pgsql-hackers-owner+archive@lists.postgresql.org
# since late 2017, pgsql-hackers-owner+M292258=...@postgresql.org before.  A
# message cross-posted to another list may carry that list's address.  The
# archive doesn't escape "From " in message bodies, so other lines that look
# like separators, mostly the "From <sha1> Mon Sep 17 00:00:00 2001" that
# starts git format-patch output, are part of the message they're in.
# Separators sometimes follow a base64 line with no blank line between.
SEPARATOR = re.compile(rb'''
    ^From\ ([\w.-]+-owner\+[^@\s]*@(?:lists\.)?postgresql\.org)
    \ +([A-Z][a-z]{2}\ [A-Z][a-z]{2}\ +\d+\ \d\d:\d\d:\d\d\ \d{4})[ \t\r]*$
    ''', re.X | re.M | re.I)
FORMAT_PATCH = re.compile(rb'^From [0-9a-f]{40} Mon Sep 17 00:00:00 2001', re.M)
HEADER_END = re.compile(rb'\r?\n\r?\n')

ENCODED_WORD = re.compile(r'=\?([^?\s]+)\?([QqBb])\?([^?\s]*)\?=')
# Names clients use for charsets that Python spells differently, or that
# are really a superset.
CHARSETS = {
    'gb2312': 'gb18030', 'gbk': 'gb18030', 'x-gbk': 'gb18030',
    'ks_c_5601-1987': 'cp949', 'euc-kr': 'cp949',
    'iso-8859-1': 'cp1252', 'latin1': 'cp1252', 'us-ascii': 'cp1252',
    'iso-8859-8-i': 'iso-8859-8', 'utf8': 'utf-8',
    'unknown-8bit': 'utf-8', 'x-unknown': 'utf-8',
}
# Charsets in which UTF-8 bytes would decode without error, so a mislabelled
# UTF-8 name would silently turn into mojibake.
SINGLE_BYTE = re.compile(r'^(cp125\d|iso8859-\d+|latin-1|koi8-[ru])$')
EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')


def bytes_to_str(raw, charset='utf-8'):
    """Decode bytes in charset, preferring UTF-8 when they're valid UTF-8."""
    charset = CHARSETS.get(charset.lower(), charset.lower())
    try:
        charset = codecs.lookup(charset).name
    except LookupError:
        charset = 'utf-8'
    if charset != 'utf-8' and SINGLE_BYTE.match(charset) and not raw.isascii():
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode(charset)
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode(charset, errors='replace')


def word_bytes(encoding, text):
    if encoding in 'Bb':
        text = text.rstrip('=')
        try:
            return base64.b64decode(text + '=' * (-len(text) % 4))
        except binascii.Error:
            return text.encode('ascii', 'replace')
    return re.sub(rb'=([0-9A-Fa-f]{2})', lambda m: bytes([int(m.group(1), 16)]),
                  text.replace('_', ' ').encode('ascii', 'replace'))


def decode_words(value):
    """
    Decode RFC 2047 encoded words anywhere in value, quotes included.

    Whitespace between two encoded words is dropped, and the bytes of
    adjacent words in the same charset are joined before decoding, since
    some clients split a multibyte character across words.
    """
    parts = []          # [str] or [(charset, bytes)]
    pos = 0
    for m in ENCODED_WORD.finditer(value):
        gap = value[pos:m.start()]
        if gap.strip() or (gap and not parts):
            parts.append(gap)
        charset = m.group(1).split('*')[0].lower()
        raw = word_bytes(m.group(2), m.group(3))
        if parts and isinstance(parts[-1], tuple) and parts[-1][0] == charset:
            parts[-1] = (charset, parts[-1][1] + raw)
        else:
            parts.append((charset, raw))
        pos = m.end()
    parts.append(value[pos:])
    return ''.join(p if isinstance(p, str) else bytes_to_str(p[1], p[0])
                   for p in parts)


def unquote(name):
    name = name.strip()
    if len(name) >= 2 and name[0] == name[-1] == '"':
        name = re.sub(r'\\(.)', r'\1', name[1:-1])
    return name.strip()


def split_from(value):
    """
    Return (name, address) from a raw From header, before decoding, so
    that an encoded or quoted comma can't confuse the split.  Handles
    'Name <addr>', '<addr>', 'addr (Name)' and a bare 'addr'.
    """
    value = value.strip()
    m = re.match(r'^(.*)<([^<>]*)>\s*$', value)
    if m:
        return unquote(m.group(1)), m.group(2).strip()
    m = re.match(r'^(\S+)\s*\((.*)\)\s*$', value)
    if m:
        return unquote(m.group(2)), m.group(1)
    return '', value


def obfuscate(text):
    """Write addresses the way the archive's web pages do."""
    return EMAIL.sub(lambda m: m.group(0).replace('@', '(at)').replace('.', '(dot)'),
                     text)


def parse_headers(block):
    """Return {lowercased name: first value} from a raw header block."""
    headers = {}
    for line in re.split(r'\r?\n(?![ \t])', bytes_to_str(block)):
        name, colon, value = line.partition(':')
        if colon and name and ' ' not in name.strip():
            headers.setdefault(name.strip().lower(), ' '.join(value.split()))
    return headers


def sent_at(date, received):
    """
    Return the Date header as 'YYYY-MM-DD HH:MM:SS +hhmm', keeping the
    sender's offset, or the archive's receipt time in UTC if there's no
    usable Date.
    """
    try:
        when = email.utils.parsedate_to_datetime(date)
    except (TypeError, ValueError, IndexError):
        when = None
    if when is None:
        return received.strftime('%Y-%m-%d %H:%M:%S +0000'), False
    return when.strftime('%Y-%m-%d %H:%M:%S ') + (when.strftime('%z') or '+0000'), True


def parse_mbox(path):
    """
    Return (rows, stats) for one month's mbox file, rows in file order.
    """
    with open(path, 'rb') as f:
        data = f.read()
    stats = collections.Counter()
    stats['format-patch lines'] = len(FORMAT_PATCH.findall(data))
    rows = []
    separators = list(SEPARATOR.finditer(data))
    if data.strip() and not separators:
        raise RuntimeError(f'{path}: no message separators found')
    for i, m in enumerate(separators):
        stats['messages'] += 1
        limit = separators[i + 1].start() if i + 1 < len(separators) else len(data)
        end = HEADER_END.search(data, m.end(), limit)
        headers = parse_headers(data[m.end():end.start() if end else limit])
        received = datetime.datetime.strptime(
            ' '.join(m.group(2).decode().split()), '%a %b %d %H:%M:%S %Y')
        when, dated = sent_at(headers.get('date', ''), received)
        if not dated:
            stats['no usable Date'] += 1
        message_id = headers.get('message-id', '')
        mid = re.search(r'<([^<>]*)>', message_id)
        message_id = (mid.group(1) if mid else message_id).strip()
        if not message_id:
            stats['no Message-ID'] += 1
        if 'from' not in headers:
            stats['no From'] += 1
        name, address = split_from(headers.get('from', ''))
        rows.append([message_id, when,
                     obfuscate(decode_words(name)), obfuscate(address)])
    return rows, stats


def escape(value):
    return (value.replace('\\', '\\\\').replace('\t', '\\t')
            .replace('\n', '\\n').replace('\r', '\\r'))


def write_tsv(path, columns, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\t'.join(columns) + '\n')
        for row in rows:
            f.write('\t'.join(escape(v) for v in row) + '\n')


def find_mboxes(directory, mailing_list):
    """Return {year: [(month, path)]} for LIST.YYYYMM files under directory."""
    name = re.compile(re.escape(mailing_list) + r'\.(\d{4})(\d\d)$')
    found = collections.defaultdict(list)
    for root, _, files in os.walk(directory):
        for f in files:
            m = name.match(f)
            if m:
                found[int(m.group(1))].append((int(m.group(2)), os.path.join(root, f)))
    for months in found.values():
        months.sort()
        dups = [m for m, n in collections.Counter(m for m, _ in months).items() if n > 1]
        if dups:
            sys.exit(f'more than one file for months {dups} of a year')
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().split('\n')[0])
    parser.add_argument('--mbox-dir', required=True,
                        help='directory to search for LIST.YYYYMM mbox files')
    parser.add_argument('--list', default='pgsql-hackers',
                        help='mailing list (default: pgsql-hackers)')
    parser.add_argument('--data', default='data',
                        help='output directory (default: data)')
    parser.add_argument('--years', type=int, nargs='+',
                        help='only write these years')
    args = parser.parse_args()

    found = find_mboxes(args.mbox_dir, args.list)
    years = sorted(y for y in found if not args.years or y in args.years)
    if not years:
        sys.exit(f'no {args.list}.YYYYMM files found under {args.mbox_dir}')
    paths = [p for y in years for _, p in found[y]]
    with concurrent.futures.ProcessPoolExecutor(os.cpu_count()) as pool:
        parsed = dict(zip(paths, pool.map(parse_mbox, paths)))

    for year in years:
        rows = []
        stats = collections.Counter()
        seen = set()
        for _, path in found[year]:
            month_rows, month_stats = parsed[path]
            stats.update(month_stats)
            for row in month_rows:
                # The archive files each message once, but guard anyway.
                if row[0] and row[0] in seen:
                    stats['duplicates dropped'] += 1
                    continue
                seen.add(row[0])
                rows.append(row)
        write_tsv(os.path.join(args.data, 'mail', args.list, f'{year}.tsv'),
                  COLUMNS, rows)
        months = [m for m, _ in found[year]]
        missing = sorted(set(range(1, 13)) - set(months))
        print(f'{year}: {len(rows)} messages'
              + ''.join(f', {n} {k}' for k, n in sorted(stats.items())
                        if k != 'messages')
              + (f'; no file for months {missing}' if missing else ''),
              file=sys.stderr)


if __name__ == '__main__':
    main()
