from glob import glob
import logging
from os.path import join, exists, split
from os import remove
import sqlite3

from gtf import load_gtf_as_dataframe
from locus import normalize_chromosome
from memory_cache import load_csv, clear_cached_objects

import datacache
import numpy as np
import pandas as pd


MIN_ENSEMBL_RELEASE = 48
MAX_ENSEMBL_RELEASE = 77

def _check_release(release):
    """
    Convert a user-provided release number into
    an integer, check to make sure it's in the
    valid range of Ensembl releases
    """
    try:
        release = int(release)
    except:
       assert False, "%s is not a valid Ensembl release" % release
    assert release >= MIN_ENSEMBL_RELEASE
    assert release <= MAX_ENSEMBL_RELEASE
    return release



# mapping from Ensembl release to which reference assembly it uses
_human_references = {}

# Ensembl release 48-54 use NCBI36 as a reference
for i in xrange(48,55):
    _human_references[i] = 'NCBI36'

# Ensembl releases 55-75 use CRCh37 as a reference
for i in xrange(55,76):
    _human_references[i] = 'GRCh37'

# Ensembl releases 76 and 77 use GRCh38
for i in xrange(76,78):
    _human_references[i] = 'GRCh38'

def _which_human_reference(release):
    release = _check_release(release)
    assert release in _human_references, \
        "No reference found for release %d" % release
    return _human_references[release]


# directory which contains GTF files, missing the release number
URL_DIR_TEMPLATE = 'ftp://ftp.ensembl.org/pub/release-%d/gtf/homo_sapiens/'
FILENAME_TEMPLATE = "Homo_sapiens.%s.%d.gtf.gz"
CACHE_SUBDIR = "ensembl"

class EnsemblRelease(object):

    def __init__(self, release):
        self.release = _check_release(release)
        self.gtf_url_dir = URL_DIR_TEMPLATE % self.release
        self.reference_name =  _which_human_reference(self.release)
        self.gtf_filename = FILENAME_TEMPLATE  % (
            self.reference_name, self.release
        )
        self.gtf_url = join(self.gtf_url_dir, self.gtf_filename)
        self._local_gtf_path = None

        # lazily load DataFrame of all GTF entries
        self._df = None

        # lazily construct sqlite3 database
        self._db = None

        self.logger = logging.getLogger()
        self.logger.setLevel(logging.INFO)

    def __str__(self):
        return "EnsemblRelease(release=%d, gtf_url='%s')" % (
            self.release, self.gtf_url)

    def __repr__(self):
        return str(self)

    def base_gtf_filename(self):
        """
        Trim extensions such as ".gtf" or ".gtf.gz",
        leaving only the base filename which should be used
        to construct other derived filenames for cached data.
        """
        assert ".gtf" in self.gtf_filename, \
            "GTF filename must contain .gtf extension: %s" % self.gtf_filename
        parts = self.gtf_filename.split(".gtf")
        return parts[0]

    def delete_cached_files(self):
        base = self.base_gtf_filename()
        dirpath = self.local_gtf_dir()
        for path in glob(join(dirpath, base + "*")):
            logging.info("Deleting cached file %s", path)
            remove(path)
        clear_cached_objects()

    def local_gtf_path(self):
        """
        Returns local path to GTF file for given release of Ensembl,
        download from the Ensembl FTP server if not already cached.
        """
        if self._local_gtf_path is None:
            self._local_gtf_path = datacache.fetch_file(
                self.gtf_url,
                filename=self.gtf_filename,
                decompress=False,
                subdir=CACHE_SUBDIR)
        assert self._local_gtf_path
        return self._local_gtf_path

    def local_gtf_dir(self):
        return split(self.local_gtf_path())[0]

    def local_csv_path(self):
        """
        Path to CSV which the annotation data with expanded columns
        for optional attributes
        """
        base = self.base_gtf_filename()
        dirpath = self.local_gtf_dir()
        csv_filename = base + ".expanded.csv"
        return join(dirpath, csv_filename)

    def local_db_filename(self):
        base = self.base_gtf_filename()
        return base + ".db"

    def local_db_path(self):
        dirpath = self.local_gtf_dir()
        filename = self.local_db_filename()
        return join(dirpath, filename)

    def local_csv_path_for_contig(self, contig_name):
        """
        Path to CSV file containing subset of Ensembl data
        restricted to given contig_name
        """
        dirpath = self.local_gtf_dir()
        base = self.base_gtf_filename()
        contig_name = normalize_chromosome(contig_name)
        return join(dirpath, base + ".contig.%s.csv" % contig_name)


    def _load_dataframe_from_gtf(self):
        """
        Parse this release's GTF file and load it as a Pandas DataFrame
        """
        path = self.local_gtf_path()
        return load_gtf_as_dataframe(path)


    def dataframe(self):
        if self._df is None:
            csv_path = self.local_csv_path()
            self._df = load_csv(csv_path, self._load_dataframe_from_gtf)
        return self._df

    def _create_database(self):
        df = self.dataframe()
        filename = self.local_db_filename()
        db = datacache.db_from_dataframe(
            db_filename=filename,
            table_name="ensembl",
            df=df,
            subdir=CACHE_SUBDIR,
            overwrite=False,
            indices = [
                ['seqname', 'start', 'end'],
                ['seqname'],
                ['gene_name'],
                ['gene_id'],
                ['transcript_id'],
                ['exon_id'],
            ])
        return db

    def _db_exists(self):
        db_path = self.local_db_path()


    def db(self):
        if self._db is None:
            db_path = self.local_db_path()
            if exists(db_path):
                db = sqlite3.connect(db_path)
            # maybe file got created but not filled
            if not datacache.db.db_table_exists(db, 'ensembl'):
                db = self._create_database()
            self._db = db
        return self._db

    def dataframe_for_contig(self, contig_name):
        """
        Load a subset of the Ensembl data for a specific contig
        """
        contig_name = normalize_chromosome(contig_name)
        contig_csv_path = self.local_csv_path_for_contig(contig_name)
        def create_dataframe():
            df = self.dataframe()
            mask = df.seqname == contig_name
            subset = df[mask]
            assert len(subset) > 0, "Contig not found: %s" % contig_name
            return subset
        return load_csv(contig_csv_path, create_dataframe)

    def dataframe_at_loci(self, contig_name, start, stop):
        """
        Subset of entries which overlap an inclusive range of loci
        """
        contig_name = normalize_chromosome(contig_name)
        df_chr = self.dataframe_for_contig(contig_name)

        # find genes whose start/end boundaries overlap with the position
        overlap_start = df_chr.start <= stop
        overlap_end = df_chr.end >= start
        overlap = overlap_start & overlap_end
        df_overlap = df_chr[overlap]
        return df_overlap


    def dataframe_column_at_loci(self, contig_name, start, stop, col_name):
        """
        Subset of entries which overlap an inclusive range of loci
        """
        contig_name = normalize_chromosome(contig_name)
        df_chr = self.dataframe_for_contig(contig_name)
        assert col_name in df_chr, "Unknown Ensembl property: %s" % col_name
        # find genes whose start/end boundaries overlap with the position
        overlap_start = df_chr.start <= stop
        overlap_end = df_chr.end >= start
        overlap = overlap_start & overlap_end
        return df_chr[col_name][overlap].dropna()

    def dataframe_at_locus(self, contig_name, position):
        return self.dataframe_at_loci(
            contig_name=contig_name,
            start=position,
            stop=position)

    def _db_cols(self, db):
        table_info = db.execute("PRAGMA table_info(ensembl);").fetchall()
        return [info[1] for info in table_info]

    def _db_col_exists(self, db, col_name):
        return col_name in self._db_cols(db)

    def db_column_values_at_loci(self, contig_name, start, stop, col_name):
        db = self.db()

        assert self._db_col_exists(db, col_name), \
            "Unknown Ensembl property: %s" % col_name
        query = """
            select distinct %s
            from ensembl
            where
                seqname='%s'
                and start <= %d
                and end >= %d
        """  % (col_name, contig_name, stop, start)
        # self.logger.info("Running query: %s" % query)
        results = db.execute(query).fetchall()
        # each result is a tuple, so pull out its first element
        return [result[0] for result in results if result[0] is not None]

    def _property_values_at_loci(self, contig_name, start, stop, property_name):
        col = self.db_column_values_at_loci(
            contig_name, start, stop, property_name)
        return list(sorted(set(col)))

    def _property_values_at_locus(self, contig_name, position, property_name):
        return self._property_values_at_loci(
            contig_name=contig_name,
            start=position,
            stop=position,
            property_name=property_name)

    def gene_ids_at_locus(self, contig_name, position):
        return self._property_values_at_locus(contig_name, position, 'gene_id')

    def gene_names_at_locus(self, contig_name, position):
        return self._property_values_at_locus(
            contig_name, position, 'gene_name')

    def exon_ids_at_locus(self, contig_name, position):
        return self._property_values_at_locus(contig_name, position, 'exon_id')

    def transcript_ids_at_locus(self, contig_name, position):
        return self._property_values_at_locus(
            contig_name, position, 'transcript_id')

    def transcript_names_at_locus(self, contig_name, position):
        return self._property_values_at_locus(
            contig_name, position, 'transcript_name')

    def gene_ids_at_loci(self, contig_name, start, stop):
        return self._property_values_at_loci(
            contig_name=contig_name,
            start=start,
            stop=stop,
            property_name='gene_id')

    def gene_names_at_loci(self, contig_name, start, stop):
        return self._property_values_at_loci(
            contig_name=contig_name,
            start=start,
            stop=stop,
            property_name='gene_name')

    def exon_ids_at_loci(self, contig_name, start, stop):
        return self._property_values_at_loci(
            contig_name=contig_name,
            start=start,
            stop=stop,
            property_name='exon_id')

    def transcript_ids_at_loci(self, contig_name, start, stop):
        return self._property_values_at_loci(
            contig_name=contig_name,
            start=start,
            stop=stop,
            property_name='transcript_id')

    def transcript_names_at_loci(self, contig_name, start, stop):
        return self._property_values_at_loci(
            contig_name=contig_name,
            start=start,
            stop=stop,
            property_name='transcript_name')

