#!/usr/bin/env python

import bamliquidator_internal.normalize_plot_and_summarize as nps 
from bamliquidator_internal.flattener import write_tab_for_all 
from bamliquidator_internal.performance_tracker import share 

import argparse
import os
import subprocess
import tables
import datetime
import csv
from time import time 

version = 0.1 # should idealy be updated before each significant git push

def create_count_table(h5file):
    class BinCount(tables.IsDescription):
        bin_number = tables.UInt32Col(    pos=0)
        cell_type  = tables.StringCol(16, pos=1)
        chromosome = tables.StringCol(16, pos=2)
        count      = tables.UInt64Col(    pos=3)
        file_name  = tables.StringCol(64, pos=4)

    table = h5file.create_table("/", "bin_counts", BinCount, "bin counts")

    table.flush()

    return table

def create_regions_table(h5file):
    class Region(tables.IsDescription):
        file_name        = tables.StringCol(64, pos=0)
        chromosome       = tables.StringCol(16, pos=1)
        region_name      = tables.StringCol(64, pos=2)
        start            = tables.UInt64Col(    pos=3)
        stop             = tables.UInt64Col(    pos=4)
        strand           = tables.StringCol(1,  pos=5)
        count            = tables.UInt64Col(    pos=6)
        normalized_count = tables.Float64Col(   pos=7)

    table = h5file.create_table("/", "region_counts", Region, "region counts")

    table.flush()

    return table

def create_lengths_table(h5file):
    class Length(tables.IsDescription):
        file_name = tables.StringCol(64, pos=0)
        length    = tables.UInt64Col(    pos=1)

    table = h5file.create_table("/", "lengths", Length, "reference sequence length")

    table.flush()

    return table
    

def all_bam_file_paths_in_directory(bam_directory):
    bam_file_paths = []
    for dirpath, _, files in os.walk(bam_directory, followlinks=True):
        for file_ in files:
            if file_.endswith(".bam"):
                bam_file_paths.append(os.path.join(dirpath, file_))
    return bam_file_paths

def bam_file_paths_with_no_counts(counts, bam_file_paths):
    with_no_counts = []

    for bam_file_path in bam_file_paths:
        count_found = False
        condition = "file_name == '%s'" % os.path.basename(bam_file_path)

        for _ in counts.where(condition):
            count_found = True

        if not count_found:
            with_no_counts.append(bam_file_path)

    return with_no_counts

# populates the lengths table and returns returns a tuple of two dictionaries:
# 1) bam_file_name -> [(chromosome, sequence length), ...] 
# 2) base_bam_file_name -> total mapped count
def populate_lengths(lengths, bam_file_paths):
    file_to_chromosome_length_pairs = {} 
    file_to_count = {} 

    chr_col         = 0
    length_col      = 1
    mapped_read_col = 2

    # this skip list is somewhat arbitrary and can be emptied/removed if desired
    chromosome_patterns_to_skip = ["chrUn", "_random", "Zv9_", "_hap"]

    for bam_file_path in bam_file_paths:
        args = ["samtools", "idxstats", bam_file_path]
        output = subprocess.check_output(args)
        # skip last two lines: the unmapped chromosome line and the empty line
        reader = csv.reader(output.split('\n')[:-2], delimiter='\t')
        file_name = os.path.basename(bam_file_path)
        file_count = 0
        
        chromosome_length_pairs = []
        for row in reader:
            chromosome = row[chr_col]
            if any(pattern in chromosome for pattern in chromosome_patterns_to_skip):
                continue
            file_count += int(row[mapped_read_col])
            chromosome_length_pairs.append((chromosome, int(row[length_col])))
        file_to_chromosome_length_pairs[file_name] = chromosome_length_pairs
        file_to_count[file_name] = file_count
        
        lengths.row["file_name"] = file_name
        lengths.row["length"] = file_count
        lengths.row.append()

    lengths.flush()

    return file_to_chromosome_length_pairs, file_to_count

def liquidate(bam_file_paths, output_directory, file_to_chromosome_length_pairs, file_to_count,
              counts_file_path, executable_path, email, bin_size = None, region_file = None, flatten = False):

    if (bin_size is not None and region_file is not None) or (bin_size is None and region_file is None):
        sys.exit("either bin_size or region_file must be provided, but not both")

    log = "bin_size=%s\n" % bin_size

    for i, bam_file_path in enumerate(bam_file_paths):
        print "Liquidating %s (file %d of %d, %s)" % (
            bam_file_path, i+1, len(bam_file_paths), datetime.datetime.now().strftime('%H:%M:%S'))
        cell_type = os.path.basename(os.path.dirname(bam_file_path))

        bam_file_name = os.path.basename(bam_file_path)

        if bin_size:
            args = [executable_path, cell_type, str(bin_size), bam_file_path, counts_file_path]

            for chromosome, length in file_to_chromosome_length_pairs[bam_file_name]:
                args.append(chromosome)
                args.append(str(length))
        else:
            args = [executable_path, region_file, bam_file_path, counts_file_path]

        start = time()
        return_code = subprocess.call(args)
        duration = time() - start
        if bin_size:
            reads = file_to_count[bam_file_name]
            rate = reads / (10**6) / duration
            print "Liquidation completed: %f seconds, %d reads, %f millions of reads per second" % (duration, reads, rate)
            log += "bin_seconds=%s,reads=%s,mrps=%s\n" % (duration, reads, rate)
        else:
            print "Liquidation completed: %f seconds" % (duration)
            log += "region_seconds=%s\n" % duration # todo: maybe include the line count of the region file?

        if return_code != 0:
            print "%s failed with exit code %d" % (executable_path, return_code)
            exit(return_code)

    counts_file = tables.open_file(counts_file_path, mode = "r+")

    start = time()
    if bin_size:
        nps.normalize_plot_and_summarize(counts_file, output_directory, bin_size) 
    else:
        nps.normalize(counts_file.root.region_counts, file_to_count)
    duration = time() - start
    log += "nps_seconds=%s\n" % duration

    if flatten:
        print "Flattening efficient HDF5 tables into inefficient text files"
        start = time()
        write_tab_for_all(counts_file, output_directory)
        duration = time() - start 
        print "Flattening took %f seconds" % duration
        log += "flatten_seconds=%s\n" % duration

    counts_file.close()

    if email:
        start = time()
        print "Emailing hardware info and performance measurements for tracking" 
        share("bamliquidator_batch", version, log)
        duration = time() - start
        print "Emailing took %f seconds" % duration
        

def main():
    parser = argparse.ArgumentParser(description='Count the number of base pair reads in each bin or region '
                                                 'in the bam file(s) at the given directory, and then normalize, plot bins, '
                                                 'and summarize the counts in the output directory.  For additional '
                                                 'help, please see https://github.com/BradnerLab/pipeline/wiki')
    parser.add_argument('-o', '--output_directory', default='output',
                        help='Directory to create and output the h5 and/or html files to (aborts if already exists). '
                             'Default is "./output".')
    parser.add_argument('-c', '--counts_file',
                        help='HDF5 counts file from a prior run to be appended to.  If unspecified, defaults to '
                             'creating a new file "counts.h5" in the output directory.')
    parser.add_argument('-b', '--bin_size', type=int, default=100000,
                        help="Number of base pairs in each bin -- the smaller the bin size the longer the runtime and "
                             "the larger the data files (default is 100000). This argument is ignored if regions are provided.")
    parser.add_argument('-r', '--regions_file',
                        help='a region file in either .gff or .bed format')
    parser.add_argument('-f', '--flatten', action='store_true',
                        help='flatten all HDF5 tables into tab delimited text files in the output directory, one for each '
                              'chromosome (note that HDF5 files can be efficiently queried and used directly -- e.g. please '
                              'see http://www.pytables.org/ for easy to use Python APIs and '
                              'http://www.hdfgroup.org/products/java/hdf-java-html/hdfview/ for an easy to use GUI for '
                              'browsing HDF5 files')
    parser.add_argument('-s', '--skip_email', default=False, action='store_true', 
                        help='skip sending performance tracking email -- these emails are sent by default during beta testing, '
                             'and will be removed (or at least not be the default) when this app leaves beta')
    parser.add_argument('bam_file_path', 
                        help='The directory to recursively search for .bam files for counting.  Every .bam file must '
                             'have a corresponding .bai file at the same location.  To count just a single file, '
                             'provide the .bam file path instead of a directory.  The parent directory of each .bam '
                             'file is interpreted as the cell type (e.g. mm1s might be an appropriate directory '
                             'name).  Bam files in the same directory are grouped together for plotting. Plots use '
                             'normalized counts, such that all .bam files in the same directory have bin '
                             'counts that add up to 1 for each chromosome.  If your .bam files are not in this '
                             'directory format, please consider creating a directory of sym links to your actual '
                             '.bam and .bai files. If the .bam file already has 1 or more reads in the HDF5 counts file, '
                             'then that .bam file is skipped from liquidation, but is still included in normalization, '
                             'plotting, and summaries.')
    args = parser.parse_args()

    assert(tables.__version__ >= '3.0.0')

    if args.regions_file is None:
        region_mode = False 
    else:
        region_mode = True
        args.bin_size = None

    executable_path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                   "bamliquidator_internal",
                                   "bamliquidator_regions" if region_mode else "bamliquidator_bins")
    if not os.path.isfile(executable_path):
        sys.exit("%s is missing -- try cd'ing into the directory and running 'make'" % executable_path)

    os.mkdir(args.output_directory)

    if args.counts_file is None:
        args.counts_file = os.path.join(args.output_directory, "counts.h5")
    
        counts_file = tables.open_file(args.counts_file, mode = "w",
                                       title = "bam liquidator genome bin read counts")
    else:
        print "appending to a pre-existing counts file is currently broken"
        exit(-1)
        counts_file = tables.open_file(args.counts_file, "r+")

    try: 
        counts = counts_file.root.region_counts if region_mode else counts_file.root.bin_counts
        lengths = counts_file.root.lengths
    except:
        counts = create_regions_table(counts_file) if region_mode else create_count_table(counts_file)
        lengths = create_lengths_table(counts_file)

    if os.path.isdir(args.bam_file_path):
        bam_file_paths = all_bam_file_paths_in_directory(args.bam_file_path)
    else:
        bam_file_paths = [args.bam_file_path]
   
    bam_file_paths = bam_file_paths_with_no_counts(counts, bam_file_paths)

    file_to_chromosome_length_pairs, file_to_count = populate_lengths(lengths, bam_file_paths)

    counts_file.close() # bamliquidator_bins/bamliquidator_regions will open this file and modify
                        # it, so it is probably best that we not hold an out of sync reference

    liquidate(bam_file_paths, args.output_directory, file_to_chromosome_length_pairs, file_to_count,
              args.counts_file, executable_path, not args.skip_email, args.bin_size, args.regions_file, args.flatten)

if __name__ == "__main__":
    main()

'''
   The MIT License (MIT) 

   Copyright (c) 2013 John DiMatteo (jdimatteo@gmail.com)

   Permission is hereby granted, free of charge, to any person obtaining a copy
   of this software and associated documentation files (the "Software"), to deal
   in the Software without restriction, including without limitation the rights
   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
   copies of the Software, and to permit persons to whom the Software is
   furnished to do so, subject to the following conditions:

   The above copyright notice and this permission notice shall be included in
   all copies or substantial portions of the Software.

   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
   THE SOFTWARE. 
'''
