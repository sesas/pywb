from pywb.utils.timeutils import iso_date_to_timestamp
from pywb.utils.bufferedreaders import DecompressingBufferedReader
from pywb.utils.canonicalize import canonicalize
from pywb.utils.loaders import extract_post_query, append_post_query

from recordloader import ArcWarcRecordLoader

import hashlib
import base64

import re
import sys

from bisect import insort


#=================================================================
class ArchiveIndexer(object):
    """ Generate a CDX index for WARC and ARC files, both gzip chunk
    compressed and uncompressed

    The indexer will automatically detect format, and decompress
    if necessary
    """

    # arc/warc record types which are
    # indexed by default, without 'include_all'
    DEFAULT_REC_TYPES = ('response', 'revisit', 'metadata', 'resource')

    def __init__(self, fileobj, filename,
                 out=sys.stdout, sort=False, writer=None, surt_ordered=True,
                 include_all=False, append_post_query=False):
        self.fh = fileobj
        self.filename = filename

        loader_parse_req = include_all or append_post_query
        self.loader = ArcWarcRecordLoader(parse_request=loader_parse_req)

        self.offset = 0
        self.known_format = None
        self.surt_ordered = surt_ordered

        self.include_all = include_all
        self.append_post_query = append_post_query

        if writer:
            self.writer = writer
        elif sort:
            self.writer = SortedCDXWriter(out)
        else:
            self.writer = CDXWriter(out)

        # todo: refactor this
        self.writer.indexer = self

        if append_post_query:
            self.writer = PostResolveWriter(self.writer, self)

    def make_index(self):
        """ Output a cdx index!
        """

        decomp_type = 'gzip'
        block_size = 16384

        reader = DecompressingBufferedReader(self.fh,
                                             block_size=block_size,
                                             decomp_type=decomp_type)
        self.offset = self.fh.tell()
        next_line = None

        self.writer.start()

        try:
            while True:
                try:
                    record = self._process_reader(reader, next_line)
                except EOFError:
                    break

                # for non-compressed, consume blank lines here
                if not reader.decompressor:
                    next_line = self._consume_blanklines(reader)
                    if next_line is None:
                        # at end of file
                        break

                # reset reader for next member
                else:
                    reader.read_next_member()
        finally:
            self.writer.end()

    def _consume_blanklines(self, reader):
        """ Consume blank lines that are between records
        - For warcs, there are usually 2
        - For arcs, may be 1 or 0
        - For block gzipped files, these are at end of each gzip envelope
          and are included in record length which is the full gzip envelope
        - For uncompressed, they are between records and so are NOT part of
          the record length
        """
        while True:
            line = reader.readline()
            if len(line) == 0:
                return None

            if line.rstrip() == '':
                self.offset = self.fh.tell() - reader.rem_length()
                continue

            return line

    def _read_to_record_end(self, reader, record):
        """ Read to end of record and update current offset,
        which is used to compute record length
        - For compressed files, blank lines are consumed
          since they are part of record length
        - For uncompressed files, blank lines are read later,
          and not included in the record length
        """

        if reader.decompressor:
            self._consume_blanklines(reader)

        self.offset = self.fh.tell() - reader.rem_length()

    def _process_reader(self, reader, next_line):
        """ Use loader to parse the record from the reader stream
        Supporting warc and arc records
        """
        record = self.loader.parse_record_stream(reader,
                                                 next_line,
                                                 self.known_format)

        # Track known format for faster parsing of other records
        self.known_format = record.format

        if record.format == 'warc':
            result = self._parse_warc_record(record)
        elif record.format == 'arc':
            result = self._parse_arc_record(record)

        if not result:
            self.read_rest(record.stream)
            self._read_to_record_end(reader, record)
            return record

        post_query = None

        if record.rec_type == 'request':
            method = record.status_headers.protocol
            mime = result[3]
            len_ = record.status_headers.get_header('Content-Length')

            post_query = extract_post_query(method,
                                            mime,
                                            len_,
                                            record.stream)

            # should be 0 if read query string
            num = self.read_rest(record.stream)
        # generate digest if it doesn't exist and if not a revisit
        # if revisit, then nothing we can do here
        elif result[-1] == '-' and record.rec_type != 'revisit':
            digester = hashlib.sha1()
            self.read_rest(record.stream, digester)
            result[-1] = base64.b32encode(digester.digest())
        else:
            num = self.read_rest(record.stream)

        result.append('- -')

        offset = self.offset
        self._read_to_record_end(reader, record)
        length = self.offset - offset

        result.append(str(length))
        result.append(str(offset))
        result.append(self.filename)

        self.writer.write(result, record.rec_type, post_query)

        return record

    def _parse_warc_record(self, record):
        """ Parse warc record to be included in index, or
        return none if skipping this type of record
        """

        if (not self.append_post_query and
            not self.include_record(record.rec_type)):
            return None

        url = record.rec_headers.get_header('WARC-Target-Uri')
        if not url:
            return None

        timestamp = record.rec_headers.get_header('WARC-Date')
        timestamp = iso_date_to_timestamp(timestamp)

        digest = record.rec_headers.get_header('WARC-Payload-Digest')

        if record.rec_type == 'revisit':
            mime = 'warc/revisit'
            status = '-'
        elif record.rec_type == 'request':
            mime = record.status_headers.get_header('Content-Type')
            mime = self._extract_mime(mime, '-')
            status = '-'
        else:
            mime = record.status_headers.get_header('Content-Type')
            mime = self._extract_mime(mime)
            status = self._extract_status(record.status_headers)

        if digest and digest.startswith('sha1:'):
            digest = digest[len('sha1:'):]

        if not digest:
            digest = '-'

        key = canonicalize(url, self.surt_ordered)

        return [key,
                timestamp,
                url,
                mime,
                status,
                digest]

    def _parse_arc_record(self, record):
        """ Parse arc record and return list of fields
        to include in index, or retur none if skipping this
        type of record
        """
        if record.rec_type == 'arc_header':
            return None

        url = record.rec_headers.get_header('uri')
        url = url.replace('\r', '%0D')
        url = url.replace('\n', '%0A')
        # replace formfeed
        url = url.replace('\x0c', '%0C')
        # replace nulls
        url = url.replace('\x00', '%00')

        timestamp = record.rec_headers.get_header('archive-date')
        if len(timestamp) > 14:
            timestamp = timestamp[:14]

        status = self._extract_status(record.status_headers)

        mime = record.rec_headers.get_header('content-type')
        mime = self._extract_mime(mime)

        key = canonicalize(url, self.surt_ordered)

        return [key,
                timestamp,
                url,
                mime,
                status,
                '-']

    MIME_RE = re.compile('[; ]')

    def _extract_mime(self, mime, def_mime='unk'):
        """ Utility function to extract mimetype only
        from a full content type, removing charset settings
        """
        if mime:
            mime = self.MIME_RE.split(mime, 1)[0]
        if not mime:
            mime = def_mime
        return mime

    def _extract_status(self, status_headers):
        status = status_headers.statusline.split(' ')[0]
        if not status:
            status = '-'
        return status

    def read_rest(self, reader, digester=None):
        """ Read remainder of the stream
        If a digester is included, update it
        with the data read
        """
        num = 0
        while True:
            b = reader.read(8192)
            if not b:
                break
            num += len(b)
            if digester:
                digester.update(b)
        return num

    def include_record(self, type_):
        return self.include_all or (type_ in self.DEFAULT_REC_TYPES)

    def add_post_query(self, fields, post_query):
        url = append_post_query(fields[2], post_query)
        fields[0] = canonicalize(url, self.surt_ordered)
        return fields


#=================================================================
class CDXWriter(object):
    def __init__(self, out):
        self.out = out
        self.indexer = None

    def start(self):
        self.out.write(' CDX N b a m s k r M S V g\n')

    def write(self, line, rec_type, *args):
        if not self.indexer or self.indexer.include_record(rec_type):
            self.out.write(' '.join(line) + '\n')

    def end(self):
        pass


#=================================================================
class SortedCDXWriter(CDXWriter):
    def __init__(self, out):
        super(SortedCDXWriter, self).__init__(out)
        self.sortlist = []

    def write(self, line, rec_type, *args):
        if not self.indexer or self.indexer.include_record(rec_type):
            line = ' '.join(line) + '\n'
            insort(self.sortlist, line)

    def end(self):
        self.out.write(''.join(self.sortlist))


#=================================================================
class PostResolveWriter(CDXWriter):
    def __init__(self, writer, indexer):
        self.writer = writer
        self.indexer = indexer
        self.prev_line = None
        self.prev_post_query = None
        self.prev_type = None

    def start(self):
        self.writer.start()

    def write(self, line, rec_type, post_query):
        if not self.prev_line:
            self.prev_line = line
            self.prev_post_query = post_query
            self.prev_type = rec_type
            return

        #cdx original field
        if self.prev_line[2] != line[2]:
            self.writer.write(self.prev_line, self.prev_type)
            self.prev_line = line
            self.prev_post_query = post_query
            return

        if self.prev_post_query or post_query:
            if self.prev_post_query:
                self.indexer.add_post_query(line, self.prev_post_query)
            else:
                self.indexer.add_post_query(line, post_query)

        # update prev url key too
        self.prev_line[0] = line[0]

        # write both lines
        self.writer.write(self.prev_line, self.prev_type)
        self.writer.write(line, rec_type)

        # flush any cached lines
        self.prev_line = None
        self.prev_post_query = None
        self.prev_type = None

    def end(self):
        if self.prev_line:
            self.writer.write(self.prev_line, self.prev_type)

        self.writer.end()


#=================================================================
class MultiFileMixin(object):
    def start_all(self):
        super(MultiFileMixin, self).start()

    def end_all(self):
        super(MultiFileMixin, self).end()

    def start(self):
        pass

    def end(self):
        pass


class MultiFileCDXWriter(MultiFileMixin, CDXWriter):
    pass


class MultiFileSortedCDXWriter(MultiFileMixin, SortedCDXWriter):
    pass


#=================================================================
import os
from argparse import ArgumentParser, RawTextHelpFormatter


def iter_file_or_dir(inputs):
    for input_ in inputs:
        if not os.path.isdir(input_):
            yield input_, os.path.basename(input_)
        else:
            for filename in os.listdir(input_):
                yield os.path.join(input_, filename), filename


def index_to_file(inputs, output, sort,
                  surt_ordered, include_all, append_post_query):
    if output == '-':
        outfile = sys.stdout
    else:
        outfile = open(output, 'w')

    if sort:
        writer = MultiFileSortedCDXWriter(outfile)
    else:
        writer = MultiFileCDXWriter(outfile)

    try:
        infile = None
        writer.start_all()

        for fullpath, filename in iter_file_or_dir(inputs):
            with open(fullpath, 'r') as infile:
                ArchiveIndexer(fileobj=infile,
                               filename=filename,
                               writer=writer,
                               surt_ordered=surt_ordered,
                               append_post_query=append_post_query,
                               include_all=include_all).make_index()
    finally:
        writer.end_all()
        if infile:
            infile.close()


def remove_ext(filename):
    for ext in ('.arc', '.arc.gz', '.warc', '.warc.gz'):
        if filename.endswith(ext):
            filename = filename[:-len(ext)]
            break

    return filename


def cdx_filename(filename):
    return remove_ext(filename) + '.cdx'


def index_to_dir(inputs, output, sort,
                 surt_ordered, include_all, append_post_query):
    for fullpath, filename in iter_file_or_dir(inputs):

        outpath = cdx_filename(filename)
        outpath = os.path.join(output, outpath)

        with open(outpath, 'w') as outfile:
            with open(fullpath, 'r') as infile:
                ArchiveIndexer(fileobj=infile,
                               filename=filename,
                               sort=sort,
                               out=outfile,
                               surt_ordered=surt_ordered,
                               append_post_query=append_post_query,
                               include_all=include_all).make_index()


def main(args=None):
    description = """
Generate .cdx index files for WARCs and ARCs
Compressed (.warc.gz / .arc.gz) or uncompressed (.warc / .arc) formats
are supported.
"""

    epilog = """
Some examples:

* Create "example.cdx" index from example.warc.gz
{0} ./cdx/example.cdx ./warcs/example.warc.gz

* Create "combined.cdx", a combined, sorted index of all warcs in ./warcs/
{0} --sort combined.cdx ./warcs/

* Create a sorted cdx per file in ./cdx/ for each archive file in ./warcs/
{0} --sort ./cdx/ ./warcs/
""".format(os.path.basename(sys.argv[0]))

    sort_help = """
sort the output to each file before writing to create a total ordering
"""

    unsurt_help = """
Convert SURT (Sort-friendly URI Reordering Transform) back to regular
urls for the cdx key. Default is to use SURT keys.
Not-recommended for new cdx, use only for backwards-compatibility.
"""

    output_help = """output file or directory.
- If directory, each input file is written to a seperate output file
  with a .cdx extension
- If output is '-', output is written to stdout
"""

    input_help = """input file or directory
- If directory, all archive files from that directory are read
"""

    allrecords_help = """include all records.
currently includes the 'request' records in addition to all
response records"""

    post_append_help = """for POST requests, append
form query to url key. (Only applies to form url encoded posts)"""

    parser = ArgumentParser(description=description,
                            epilog=epilog,
                            formatter_class=RawTextHelpFormatter)

    parser.add_argument('-s', '--sort',
                        action='store_true',
                        help=sort_help)

    parser.add_argument('-a', '--allrecords',
                        action='store_true',
                        help=allrecords_help)

    parser.add_argument('-p', '--postappend',
                        action='store_true',
                        help=post_append_help)

    parser.add_argument('-u', '--unsurt',
                        action='store_true',
                        help=unsurt_help)

    parser.add_argument('output', nargs='?', default='-', help=output_help)
    parser.add_argument('inputs', nargs='+', help=input_help)

    cmd = parser.parse_args(args=args)
    if cmd.output != '-' and os.path.isdir(cmd.output):
        index_to_dir(cmd.inputs, cmd.output, cmd.sort,
                     not cmd.unsurt, cmd.allrecords, cmd.postappend)
    else:
        index_to_file(cmd.inputs, cmd.output, cmd.sort,
                      not cmd.unsurt, cmd.allrecords, cmd.postappend)


if __name__ == '__main__':
    main()