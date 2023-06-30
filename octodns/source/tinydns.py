#
#
#

import logging
import re
import textwrap
from collections import defaultdict
from ipaddress import ip_address
from os import listdir
from os.path import join
from pprint import pprint

from ..record import Record
from ..zone import DuplicateRecordException, SubzoneRecordException
from .base import BaseSource


class TinyDnsBaseSource(BaseSource):
    # spec https://cr.yp.to/djbdns/tinydns-data.html
    # ipv6 addon spec https://docs.bytemark.co.uk/article/tinydns-format/
    SUPPORTS_GEO = False
    SUPPORTS_DYNAMIC = False
    SUPPORTS = set(('A', 'CNAME', 'MX', 'NS', 'TXT', 'AAAA'))

    def __init__(self, id, default_ttl=3600):
        super().__init__(id)
        self.default_ttl = default_ttl

    def _data_for_A(self, _type, records):
        values = []
        for record in records:
            if record[0] != '0.0.0.0':
                values.append(record[0])
        if len(values) == 0:
            return
        try:
            ttl = records[0][1]
        except IndexError:
            ttl = self.default_ttl
        return {'ttl': ttl, 'type': _type, 'values': values}

    def _data_for_AAAA(self, _type, records):
        values = []
        for record in records:
            # TinyDNS files have the ipv6 address written in full, but with the
            # colons removed. This inserts a colon every 4th character to make
            # the address correct.
            values.append(u":".join(textwrap.wrap(record[0], 4)))
        try:
            ttl = records[0][1]
        except IndexError:
            ttl = self.default_ttl
        return {'ttl': ttl, 'type': _type, 'values': values}

    def _data_for_TXT(self, _type, records):
        values = []

        for record in records:
            new_value = (
                record[0]
                .encode('latin1')
                .decode('unicode-escape')
                .replace(";", "\\;")
            )
            values.append(new_value)

        try:
            ttl = records[0][1]
        except IndexError:
            ttl = self.default_ttl
        return {'ttl': ttl, 'type': _type, 'values': values}

    def _data_for_CNAME(self, _type, records):
        first = records[0]
        try:
            ttl = first[1]
        except IndexError:
            ttl = self.default_ttl
        return {'ttl': ttl, 'type': _type, 'value': f'{first[0]}.'}

    def _data_for_MX(self, _type, records):
        try:
            ttl = records[0][2]
        except IndexError:
            ttl = self.default_ttl
        return {
            'ttl': ttl,
            'type': _type,
            'values': [
                {'preference': r[1], 'exchange': f'{r[0]}.'} for r in records
            ],
        }

    def _data_for_NS(self, _type, records):
        try:
            ttl = records[0][1]
        except IndexError:
            ttl = self.default_ttl
        return {
            'ttl': ttl,
            'type': _type,
            'values': [f'{r[0]}.' for r in records],
        }

    def populate(self, zone, target=False, lenient=False):
        self.log.debug(
            'populate: name=%s, target=%s, lenient=%s',
            zone.name,
            target,
            lenient,
        )

        before = len(zone.records)

        if zone.name.endswith('in-addr.arpa.'):
            self._populate_in_addr_arpa(zone, lenient)
        else:
            self._populate_normal(zone, lenient)

        self.log.info(
            'populate:   found %s records', len(zone.records) - before
        )

    def _records_for_at(self, zone, name, lines, in_addr=False):
        # @fqdn:ip:x:dist:ttl:timestamp:lo
        # MX (and optional A)
        if in_addr:
            return []

        # see if we can find a ttl on any of the lines, first one wins
        ttl = self.default_ttl
        for line in lines:
            try:
                ttl = int(lines[0][4])
                break
            except IndexError:
                pass

        values = []
        for line in lines:
            mx = line[2]
            # if there's a . in the mx we hit a special case and use it as-is
            if '.' not in mx:
                # otherwise we treat it as the MX hostnam and construct the rest
                mx = f'{mx}.ns.{zone.name}'
            elif mx[-1] != '.':
                mx = f'{mx}.'

            # default distance is 0
            try:
                dist = line[3] or 0
            except IndexError:
                dist = 0

            # if we have an IP then we need to create an A for the MX
            ip = line[1]
            if ip:
                mx_name = zone.hostname_from_fqdn(mx)
                yield 'A', mx_name, [ip]

            values.append({'preference': dist, 'exchange': mx})

        yield 'MX', name, values

    def _records_for_C(self, zone, name, lines, in_addr=False):
        # Cfqdn:p:ttl:timestamp:lo
        # CNAME
        if in_addr:
            return []

        value = lines[0][1]
        if value[-1] != '.':
            value = f'{value}.'

        # see if we can find a ttl on any of the lines, first one wins
        ttl = self.default_ttl
        for line in lines:
            try:
                ttl = int(lines[0][2])
                break
            except IndexError:
                pass

        yield 'CNAME', name, [value]

    def _records_for_caret(self, zone, name, lines, in_addr=False):
        # .fqdn:ip:x:ttl:timestamp:lo
        # NS (and optional A)
        if not in_addr:
            return []

        raise NotImplementedError()

    def _records_for_equal(self, zone, name, lines, in_addr=False):
        # =fqdn:ip:ttl:timestamp:lo
        # A (in_addr False) & PTR (in_addr True)
        yield from self._records_for_plus(zone, name, lines, in_addr)
        yield from self._records_for_caret(zone, name, lines, in_addr)

    def _records_for_dot(self, zone, name, lines, in_addr=False):
        # .fqdn:ip:x:ttl:timestamp:lo
        # NS (and optional A)
        if in_addr:
            return []

        # see if we can find a ttl on any of the lines, first one wins
        ttl = self.default_ttl
        for line in lines:
            try:
                ttl = int(lines[0][3])
                break
            except IndexError:
                pass

        values = []
        for line in lines:
            ns = line[2]
            # if there's a . in the ns we hit a special case and use it as-is
            if '.' not in ns:
                # otherwise we treat it as the NS hostnam and construct the rest
                ns = f'{ns}.ns.{zone.name}'
            elif ns[-1] != '.':
                ns = f'{ns}.'

            # if we have an IP then we need to create an A for the MX
            ip = line[1]
            if ip:
                ns_name = zone.hostname_from_fqdn(ns)
                yield 'A', ns_name, [ip]

            values.append(ns)

        yield 'NS', name, values

    _records_for_amp = _records_for_dot

    def _records_for_plus(self, zone, name, lines, in_addr=False):
        # +fqdn:ip:ttl:timestamp:lo
        # A
        if in_addr:
            return []

        # collect our ip(s)
        ips = [l[1] for l in lines if l[1] != '0.0.0.0']

        if not ips:
            # we didn't find any value ips so nothing to do
            return []

        # see if we can find a ttl on any of the lines, first one wins
        ttl = self.default_ttl
        for line in lines:
            try:
                ttl = int(lines[0][2])
                break
            except IndexError:
                pass

        yield 'A', name, ips

    def _records_for_quote(self, zone, name, lines, in_addr=False):
        # 'fqdn:s:ttl:timestamp:lo
        # TXT
        if in_addr:
            return []

        # collect our ip(s)
        values = [
            l[1].encode('latin1').decode('unicode-escape').replace(";", "\\;")
            for l in lines
        ]

        # see if we can find a ttl on any of the lines, first one wins
        ttl = self.default_ttl
        for line in lines:
            try:
                ttl = int(lines[0][2])
                break
            except IndexError:
                pass

        yield 'TXT', name, values

    def _records_for_three(self, zone, name, lines, in_addr=False):
        # 3fqdn:ip:ttl:timestamp:lo
        # AAAA
        if in_addr:
            return []

        # collect our ip(s)
        ips = []
        for line in lines:
            # TinyDNS files have the ipv6 address written in full, but with the
            # colons removed. This inserts a colon every 4th character to make
            # the address correct.
            ips.append(u':'.join(textwrap.wrap(line[1], 4)))

        # see if we can find a ttl on any of the lines, first one wins
        ttl = self.default_ttl
        for line in lines:
            try:
                ttl = int(lines[0][2])
                break
            except IndexError:
                pass

        yield 'AAAA', name, ips

    def _records_for_six(self, zone, name, lines, in_addr=False):
        # 6fqdn:ip:ttl:timestamp:lo
        # AAAA (in_addr False) & PTR (in_addr True)
        yield from self._records_for_three(zone, name, lines, in_addr)
        yield from self._records_for_caret(zone, name, lines, in_addr)

    SYMBOL_MAP = {
        '=': _records_for_equal,  # A
        '^': _records_for_caret,  # PTR
        '.': _records_for_dot,  # NS
        'C': _records_for_C,  # CNAME
        '+': _records_for_plus,  # A
        '@': _records_for_at,  # MX
        '&': _records_for_amp,  # NS
        '\'': _records_for_quote,  # TXT
        '3': _records_for_three,  # AAAA
        '6': _records_for_six,  # AAAA
        # TODO:
        #'S': _records_for_S, # SRV
        # Sfqdn:ip:x:port:priority:weight:ttl:timestamp:lo
        #':': _record_for_semicolon # arbitrary
        # :fqdn:n:rdata:ttl:timestamp:lo
    }

    def _process_lines(self, zone, lines):
        name_re = re.compile(fr'((?P<name>.+)\.)?{zone.name[:-1]}\.?$')

        data = defaultdict(lambda: defaultdict(list))
        for line in lines:
            symbol = line[0]

            # Skip type, remove trailing comments, and omit newline
            line = line[1:].split('#', 1)[0]
            # Split on :'s including :: and strip leading/trailing ws
            line = [p.strip() for p in line.split(':')]
            # make sure the name portion matches the zone we're currently
            # working on
            name = line[0]
            if not name_re.match(name):
                self.log.info('skipping name %s, not a match, %s: ', name, line)
                continue
            # remove the zone name
            name = zone.hostname_from_fqdn(name)
            data[symbol][name].append(line)

        return data

    def _process_symbols(self, zone, symbols):
        types = defaultdict(lambda: defaultdict(list))
        ttls = defaultdict(lambda: defaultdict(lambda: self.default_ttl))
        for symbol, names in symbols.items():
            records_for = self.SYMBOL_MAP.get(symbol, None)
            if not records_for:
                # Something we don't care about
                self.log.info(
                    'skipping type %s, not supported/interested', symbol
                )
                continue

            for name, lines in names.items():
                for _type, name, values in records_for(self, zone, name, lines):
                    types[_type][name].extend(values)

        return types, ttls

    def _populate_normal(self, zone, lenient):
        # This is complicate b/c the mapping between tinydns line types (called
        # symbols here) is not one to one with (octoDNS) records. Some lines
        # create multiple types of records and multiple lines are often combined
        # to make a single record (with multiple values.) Sometimes both happen.
        # To deal with this we'll do things in 3 stages:

        # first group lines by their symbol and name
        symbols = self._process_lines(zone, self._lines())
        pprint({'symbols': symbols})

        # then work through those to group values by their _type and name
        types, ttls = self._process_symbols(zone, symbols)
        pprint({'types': types, 'ttls': ttls})

        # now we finally have all the values for each (soon to be) record
        # collected together, turn them into their coresponding record and add
        # it to the zone
        for _type, names in types.items():
            for name, values in names.items():
                data = {'ttl': ttls[_type][name], 'type': _type}
                if len(values) > 1:
                    data['values'] = values
                else:
                    data['value'] = values[0]
                pprint({'name': name, 'data': data})
                record = Record.new(zone, name, data, lenient=lenient)
                try:
                    zone.add_record(record, lenient=lenient)
                except SubzoneRecordException:
                    self.log.debug(
                        '_populate_normal: skipping subzone record=%s', record
                    )

    def _populate_in_addr_arpa(self, zone, lenient):
        name_re = re.compile(fr'(?P<name>.+)\.{zone.name[:-1]}\.?$')

        for line in self._lines():
            _type = line[0]
            # We're only interested in = (A+PTR), and ^ (PTR) records
            if _type not in ('=', '^', '&'):
                continue

            # Skip type, remove trailing comments, and omit newline
            line = line[1:].split('#', 1)[0]
            # Split on :'s including :: and strip leading/trailing ws
            line = [p.strip() for p in self.split_re.split(line)]

            if line[0].endswith('in-addr.arpa'):
                # since it's already in in-addr.arpa format
                match = name_re.match(line[0])
                value = line[1]
            else:
                addr = ip_address(line[1])
                match = name_re.match(addr.reverse_pointer)
                value = line[0]

            if match:
                try:
                    ttl = line[2]
                except IndexError:
                    ttl = self.default_ttl

                if value[-1] != '.':
                    value = f'{value}.'

                name = match.group('name')
                record = Record.new(
                    zone,
                    name,
                    {'ttl': ttl, 'type': 'PTR', 'value': value},
                    source=self,
                    lenient=lenient,
                )
                try:
                    zone.add_record(record, lenient=lenient)
                except DuplicateRecordException:
                    self.log.warning(
                        f'Duplicate PTR record for {addr}, skipping'
                    )


class TinyDnsFileSource(TinyDnsBaseSource):
    '''
    A basic TinyDNS zonefile importer created to import legacy data.

    tinydns:
        class: octodns.source.tinydns.TinyDnsFileSource
        # The location of the TinyDNS zone files
        directory: ./zones
        # The ttl to use for records when not specified in the data
        # (optional, default 3600)
        default_ttl: 3600

    NOTE: timestamps & lo fields are ignored if present.
    '''

    def __init__(self, id, directory, default_ttl=3600):
        self.log = logging.getLogger(f'TinyDnsFileSource[{id}]')
        self.log.debug(
            '__init__: id=%s, directory=%s, default_ttl=%d',
            id,
            directory,
            default_ttl,
        )
        super().__init__(id, default_ttl)
        self.directory = directory
        self._cache = None

    def _lines(self):
        if self._cache is None:
            # We unfortunately don't know where to look since tinydns stuff can
            # be defined anywhere so we'll just read all files
            lines = []
            for filename in listdir(self.directory):
                if filename[0] == '.':
                    # Ignore hidden files
                    continue
                with open(join(self.directory, filename), 'r') as fh:
                    lines += [l for l in fh.read().split('\n') if l]

            self._cache = lines

        return self._cache
