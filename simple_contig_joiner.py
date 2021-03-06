#!/usr/bin/env python
"""Stitch contigs together by means of Mummer's nucmer and fill gaps
with given reference sequence
"""

#--- standard library imports
#
import sys
import os
import logging
import argparse
import subprocess
import tempfile
import shutil
from itertools import groupby
from collections import namedtuple
try:
    if sys.version_info.major == 2:
        from string import maketrans
    else:
        maketrans = str.maketrans
except AttributeError:
    sys.stderr.write("FATAL: Unsupported Python version!\n")
    raise

#--- third-party imports
#
# /

#--- project specific imports
#
# /


__author__ = "Andreas Wilm"
__version__ = "0.2"
__email__ = "wilma@gis.a-star.edu.sg"
__license__ = "WTFPL http://www.wtfpl.net/"


# http://docs.python.org/library/logging.html
LOG = logging.getLogger("")
logging.basicConfig(level=logging.INFO,
                    format='%(levelname)s [%(asctime)s]: %(message)s')


TilingContig = namedtuple('TilingContig', [
    'ref_name', 'ref_start', 'ref_end', 'gap2next',
    'len', 'aln_cov', 'perc_ident', 'ori', 'name'])


def rev_comp(dna):
    """compute reverse complement for dna

    No support for RNA.

    >>> rev_comp("AaGgCcTtNn")
    'nNaAgGcCtT'
    """

    # maketrans doc: "Don't use strings derived from lowercase and
    # uppercase as arguments; in some locales, these don't have the
    # same length. For case conversions, always use str.lower() and
    # str.upper()."
    old_chars = "ACGTN"
    old_chars += str.lower(old_chars)
    replace_chars = "TGCAN"
    replace_chars += str.lower(replace_chars)
    trans = maketrans(old_chars, replace_chars)

    return dna.translate(trans)[::-1]


def fasta_iter(fasta_name):
    """
    Given a fasta file. yield tuples of header, sequence

    Author: Brent Pedersen
    https://www.biostars.org/p/710/
    """
    fa_fh = open(fasta_name)
    # ditch the boolean (x[0]) and just keep the header or sequence since
    # we know they alternate.
    faiter = (x[1] for x in groupby(fa_fh, lambda line: line[0] == ">"))
    for header in faiter:
        # drop the ">"
        header = next(header)[1:].strip()
        # join all sequence lines to one.
        seq = "".join(s.strip() for s in next(faiter))
        yield header, seq
    fa_fh.close()


def cmdline_parser():
    """
    creates argparse instance
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-v', '--verbose',
                        action='count', default=0)
    parser.add_argument('-q', '--quiet',
                        action='count', default=0)
    parser.add_argument("-c", "--c",
                        required=True,
                        dest="fcontigs",
                        help="Input file containing contigs to join (fasta)")
    parser.add_argument("-r", "--ref",
                        required=True,
                        dest="fref",
                        help="Reference sequence file (fasta)")
    parser.add_argument("-o", "--output",
                        default='-',
                        dest="fout",
                        help="output file (fasta; '-'=stdout=default")
    parser.add_argument("--keep-tmp-files",
                        dest="keep_temp_files",
                        action="store_true",
                        help="Don't delete temporary files")
    parser.add_argument("--tmp-dir",
                        dest="tmp_dir", # type="string|int|float"
                        help="directory to save temp files in")
    parser.add_argument("-n", "--dont-fill-with-ref",
                        dest="dont_fill_with_ref",
                        action="store_true",
                        help="Don't fill gaps with reference (keep Ns)")

    return parser


def nucmer_in_path():
    """check whether nucmer is in path
    """
    try:
        _res = subprocess.check_output(["nucmer", "-V"], stderr=subprocess.STDOUT)
    except OSError:
        return False
    else:
        return True


def run_nucmer(fref, fcontigs, out_prefix, nucmer="nucmer"):
    """Run's nucmer on given reference and query (contigs).
    Returns path to delta file"""

    fdelta = out_prefix + ".delta"
    cmd = [nucmer, fref, fcontigs, '-p', out_prefix]
    LOG.debug("Calling %s", cmd)
    try:
        o = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode('utf-8')
    except (subprocess.CalledProcessError, OSError):
        LOG.fatal("The following command failed: %s\n", " ".join(cmd))
        raise
    assert os.path.exists(fdelta), (
        "Expected file '{}' missing. Command was: '{}'. Output was '{}'".format(
            fdelta, ' '.join(cmd), o))
    return fdelta


def run_showtiling(fdelta):
    """Run mummer's show-tiling which creates a pseudo molecule from
    the contigs, filled with Ns. Return path to pseudo molecule
    and tiling file
    """

    fpseudo = fdelta + ".pseudo.fa"
    ftiling = fdelta + ".tiling.txt"
    cmd = ['show-tiling', '-p', fpseudo, fdelta]
    LOG.debug("Calling %s", cmd)
    try:
        o = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode('utf-8')
    except (subprocess.CalledProcessError, OSError):
        LOG.fatal("The following command failed: %s\n", " ".join(cmd))
        raise
    assert os.path.exists(fpseudo), (
        "Expected file '{}' missing. Command was: '{}'. Output was '{}'".format(
            fpseudo, ' '.join(cmd), o))
    with open(ftiling, 'w') as tile_fh:
        tile_fh.write(o)
    LOG.info("Pseudo molecule written to '%s'", fpseudo)
    LOG.info("Tiling info written to '%s'", ftiling)
    return fpseudo, ftiling


def parse_tiling(tiling_file):
    """Parse Nucmer's tiling file and yields TilingContig's

    function returns refstart and refstart zero-based half open (i.e.
    in slice notation)

    From Mummers doc: Standard output has an 8 column list per
    mapped contig, separated by the FastA headers of each
    reference sequence. These columns are as follows: [1] start in
    the reference [2] end in the reference [3] gap between this
    contig and the next [4] length of this contig [5] alignment
    coverage of this contig [6] average percent identity of this
    contig [7] contig orientation [8] contig ID.

    """

    with open(tiling_file) as tile_fh:
        for line in tile_fh:
            if line.startswith(">"):
                ref_name = line[1:].split()[0].strip()
                continue

            (ref_start, ref_end, gap2next, contig_len, aln_cov,
             perc_ident, ori, name) = line.strip().split("\t")
            assert ori in "+-"
            (ref_start, ref_end, gap2next, contig_len) = (
                int(x) for x in (ref_start, ref_end, gap2next, contig_len))
            (aln_cov, perc_ident) = (float(x) for x in (aln_cov, perc_ident))
            ref_start -= 1# refstart: 0-based, refend: exclusive
            yield TilingContig._make([
                ref_name, ref_start, ref_end, gap2next, contig_len,
                aln_cov, perc_ident, ori, name])


def merge_contigs_and_ref(contig_seqs, ref_seq, tiling_file, out_file):
    """Merged contigs and reference based tiling data
    """

    if out_file == "-":
        out_fh = sys.stdout
    else:
        out_fh = open(out_file, 'w')

    out_fh.write(">joined\n")
    last_refend = 0# exclusive
    contig = None
    last_refname = None
    for contig in parse_tiling(tiling_file):
        assert contig.ref_name in ref_seq, (
            "Tiling reference name '{}' not found in refseqs".format(
                contig.ref_name))
        assert contig.ref_name == last_refname or last_refname is None, (
            "No support for multiple references")
        last_refname = contig.ref_name

        # if there was a gap before this contig, fill with reference
        if contig.ref_start > last_refend:
            LOG.debug("ref %s+1:%s", last_refend, contig.ref_start)
            sq = ref_seq[contig.ref_name][last_refend:contig.ref_start]
            out_fh.write(sq)

        # if there's overlap with the next contig we clip the current
        # one (assumes all contigs are equally good)
        printto = None
        if contig.gap2next < 0:
            printto = contig.gap2next
        if contig.ori == '+':
            LOG.debug("con+ %s+1:%s", 0, printto)
            sq = contig_seqs[contig.name][:printto]
        elif contig.ori == '-':
            LOG.debug("con- %s+1:%s", 0, printto)
            sq = rev_comp(contig_seqs[contig.name])[:printto]
        else:
            raise ValueError(contig.ori)
        out_fh.write(sq)
        last_refend = contig.ref_end

    if contig is not None:
        if last_refend < len(ref_seq[contig.ref_name]):
            LOG.debug("ref %s+1:", last_refend)
            sq = ref_seq[contig.ref_name][last_refend:]
            out_fh.write(sq)
    else:
        LOG.critical("Nothing to join")
        if out_fh != sys.stdout:
            out_fh.close()
            os.unlink(out_file)
        raise ValueError(tiling_file)
    out_fh.write("\n")
    if out_fh != sys.stdout:
        out_fh.close()


def main():
    """
    The main function
    """

    parser = cmdline_parser()
    args = parser.parse_args()

    logging_level = logging.WARN + 10*args.quiet - 10*args.verbose
    LOG.setLevel(logging_level)

    for fname in [args.fref, args.fcontigs]:
        if not os.path.exists(fname):
            LOG.fatal("file '%s' does not exist.", fname)
            sys.exit(1)
    for fname in [args.fout]:
        if fname != "-" and os.path.exists(fname):
            LOG.fatal("Refusing to overwrite existing file %s'.", fname)
            sys.exit(1)

    if not nucmer_in_path():
        LOG.fatal("Couldn't find nucmer in PATH")
        sys.exit(1)


    tmp_files = []

    # run mummer's nucmer
    #
    out_prefix = tempfile.NamedTemporaryFile(
        delete=False, dir=args.tmp_dir).name
    fdelta = run_nucmer(args.fref, args.fcontigs, out_prefix)
    tmp_files.append(fdelta)
    LOG.info("Delta written to '%s'", fdelta)

    fpseudo, ftiling = run_showtiling(fdelta)
    tmp_files.extend([fpseudo, ftiling])

    if args.dont_fill_with_ref:
        LOG.info("Not replacing gaps with ref. Copying to '%s'", args.fout)
        if args.out == "-":
            with open(args.fout) as fh:
                for line in fh:
                    print(line)
        else:
            shutil.copyfile(fpseudo, args.fout)
        if not args.keep_temp_files:
            for f in tmp_files:
                os.unlink(f)
        else:
            LOG.info("Not deleting temp files")
        sys.exit(0)


    # load reference and contigs
    #
    ref_seq = dict((x[0].split()[0], x[1])
                   for x in fasta_iter(args.fref))
    assert len(ref_seq) == 1, ("Only one reference sequence supported for N filling")
    contigs = dict((x[0].split()[0], x[1])
                   for x in fasta_iter(args.fcontigs))
    try:
        merge_contigs_and_ref(contigs, ref_seq, ftiling, args.fout)
    except ValueError:
        sys.exit(1)



if __name__ == "__main__":
    main()
    LOG.info("Successful program exit")
