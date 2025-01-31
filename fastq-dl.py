#! /usr/bin/env python3
import argparse
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import requests
from executor import ExternalCommand, ExternalCommandFailed

PROGRAM = "fastq-dl"
VERSION = "1.1.0"
STDOUT = 11
STDERR = 12
ENA_FAILED = "ENA_NOT_FOUND"
SRA_FAILED = "SRA_NOT_FOUND"
MB = 1_048_576
BUFFER_SIZE = 10 * MB
logging.addLevelName(STDOUT, "STDOUT")
logging.addLevelName(STDERR, "STDERR")

ENA_URL = "https://www.ebi.ac.uk/ena/portal/api/search?result=read_run&format=tsv"
FIELDS = [
    "study_accession",
    "secondary_study_accession",
    "sample_accession",
    "secondary_sample_accession",
    "experiment_accession",
    "run_accession",
    "submission_accession",
    "tax_id",
    "scientific_name",
    "instrument_platform",
    "instrument_model",
    "library_name",
    "library_layout",
    "nominal_length",
    "library_strategy",
    "library_source",
    "library_selection",
    "read_count",
    "base_count",
    "center_name",
    "first_public",
    "last_updated",
    "experiment_title",
    "study_title",
    "study_alias",
    "experiment_alias",
    "run_alias",
    "fastq_bytes",
    "fastq_md5",
    "fastq_ftp",
    "fastq_aspera",
    "fastq_galaxy",
    "submitted_bytes",
    "submitted_md5",
    "submitted_ftp",
    "submitted_aspera",
    "submitted_galaxy",
    "submitted_format",
    "sra_bytes",
    "sra_md5",
    "sra_ftp",
    "sra_aspera",
    "sra_galaxy",
    "cram_index_ftp",
    "cram_index_aspera",
    "cram_index_galaxy",
    "sample_alias",
    "broker_name",
    "sample_title",
    "nominal_sdev",
    "first_created",
]


def set_log_level(error, debug):
    """Set the output log level."""
    return logging.ERROR if error else logging.DEBUG if debug else logging.INFO


def get_log_level():
    """Return logging level name."""
    return logging.getLevelName(logging.getLogger().getEffectiveLevel())


def execute(
    cmd,
    directory=os.getcwd(),
    capture_stdout=False,
    stdout_file=None,
    stderr_file=None,
    max_attempts=1,
    is_sra=False,
):
    """A simple wrapper around executor."""
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        try:
            command = ExternalCommand(
                cmd,
                directory=directory,
                capture=True,
                capture_stderr=True,
                stdout_file=stdout_file,
                stderr_file=stderr_file,
            )

            command.start()
            if get_log_level() == "DEBUG":
                logging.log(STDOUT, command.decoded_stdout)
                logging.log(STDERR, command.decoded_stderr)

            if capture_stdout:
                return command.decoded_stdout
            else:
                return command.returncode
        except ExternalCommandFailed as error:
            logging.error(f'"{cmd}" return exit code {command.returncode}')

            if is_sra and command.returncode == 3:
                # The FASTQ isn't on SRA for some reason, try to download from ENA
                error_msg = command.decoded_stderr.split("\n")[0]
                logging.error(error_msg)
                return SRA_FAILED

            if attempt < max_attempts:
                logging.error(f"Retry execution ({attempt} of {max_attempts})")
                time.sleep(10)
            else:
                raise error


def check_sratools():
    """Check whether the use has completed the interactive step for sra-toolkit."""
    needs_setup = False
    home = Path(os.environ["HOME"])
    ncbi_home = home / ".ncbi"
    ncbi_user = ncbi_home / "user-settings.mkfg"
    if not ncbi_home.exists():
        logging.info(f'\tDirectory "{ncbi_home}" not found, setting up.')
        ncbi_home.mkdir(parents=True)
        needs_setup = True
    elif not ncbi_user.exists():
        logging.info(f'\tFile "{ncbi_user}" not found, setting up.')
        needs_setup = True
    else:
        with open(ncbi_user, "rt") as ncbi_fh:
            uuid_found = False
            for line in ncbi_fh:
                if "/LIBS/GUID" in line:
                    uuid_found = True
        if not uuid_found:
            needs_setup = True
            logging.info(f'\tUUID not found in "{ncbi_user}", setting up.')

    if needs_setup:
        uuid = str(uuid4())
        ncbi_user.touch()
        with open(ncbi_user, "a") as ncbi_fh:
            ncbi_fh.write(f'/LIBS/GUID = "{uuid}"\n')
        logging.info(f"\tAdded randomly generated UUID to {ncbi_user}")


def sra_download(accession, outdir, cpus=1, max_attempts=10):
    """Download FASTQs from SRA using fasterq-dump."""
    check_sratools()
    fastqs = {"r1": "", "r2": "", "single_end": True}
    se = f"{outdir}/{accession}.fastq.gz"
    pe = f"{outdir}/{accession}_2.fastq.gz"

    if not os.path.exists(se) and not os.path.exists(pe):
        Path(outdir).mkdir(parents=True, exist_ok=True)
        outcome = execute(
            f"fasterq-dump {accession} --split-files --threads {cpus}",
            max_attempts=max_attempts,
            directory=outdir,
            is_sra=True,
        )
        if outcome == SRA_FAILED:
            return outcome
        else:
            execute(f"pigz --force -p {cpus} -n --fast *.fastq", directory=outdir)

    if os.path.exists(f"{outdir}/{accession}_2.fastq.gz"):
        # Paired end
        fastqs["r1"] = f"{outdir}/{accession}_1.fastq.gz"
        fastqs["r2"] = f"{outdir}/{accession}_2.fastq.gz"
        fastqs["single_end"] = False
    else:
        fastqs["r1"] = f"{outdir}/{accession}.fastq.gz"

    return fastqs


def ena_download(run, outdir, aspera=None, max_attempts=10, ftp_only=False):
    fastqs = {"r1": "", "r2": "", "single_end": True}
    fasp = run["fastq_aspera"]
    ftp = run["fastq_ftp"]
    if not ftp and not fasp:
        return ENA_FAILED

    fasp = fasp.split(";")
    ftp = ftp.split(";")
    md5 = run["fastq_md5"].split(";")
    for i in range(len(fasp)):
        is_r2 = False
        # If run is paired only include *_1.fastq and *_2.fastq, rarely a
        # run can have 3 files.
        # Example:ftp://ftp.sra.ebi.ac.uk/vol1/fastq/ERR114/007/ERR1143237
        if run["library_layout"] == "PAIRED":
            if fasp[i].endswith("_2.fastq.gz"):
                # Example: ERR1143237_2.fastq.gz
                is_r2 = True
            elif fasp[i].endswith("_1.fastq.gz"):
                # Example: ERR1143237_1.fastq.gz
                pass
            else:
                # Example: ERR1143237.fastq.gz
                # Not a part of the paired end read, so skip this file. Or,
                # its the only fastq file, and its not a paired
                obs_fq = os.path.basename(fasp[i])
                exp_fq = f'{run["run_accession"]}.fastq.gz'
                if len(fasp) != 1 and obs_fq != exp_fq:
                    continue

        # Download Run
        if md5[i]:
            fastq = download_ena_fastq(
                fasp[i],
                ftp[i],
                outdir,
                md5[i],
                aspera,
                max_attempts=max_attempts,
                ftp_only=ftp_only,
            )

            if is_r2:
                fastqs["r2"] = fastq
                fastqs["single_end"] = False
            else:
                fastqs["r1"] = fastq

    return fastqs


def md5sum(fastq):
    """Return the MD5SUM of an input file.
    Taken from https://stackoverflow.com/a/3431838/5299417
    """
    if os.path.exists(fastq):
        hash_md5 = hashlib.md5()
        with open(fastq, "rb") as fp:
            for chunk in iter(lambda: fp.read(BUFFER_SIZE), b""):
                hash_md5.update(chunk)

        return hash_md5.hexdigest()
    else:
        return None


def download_ena_fastq(fasp, ftp, outdir, md5, aspera, max_attempts=10, ftp_only=False):
    """Download FASTQs from ENA using Apera Connect or FTP."""
    success = False
    attempt = 0
    fastq = f"{outdir}/{os.path.basename(fasp)}"

    if not os.path.exists(fastq):
        Path(outdir).mkdir(parents=True, exist_ok=True)

        while not success:
            if ftp_only:
                logging.info(f"\t\tFTP download attempt {attempt + 1}")
                execute(
                    f"wget --quiet -O {fastq} ftp://{ftp}", max_attempts=max_attempts
                )
            else:
                logging.info(f"\t\tAspera Connect download attempt {attempt + 1}")
                execute(
                    (
                        f'{aspera["ascp"]} -QT -l {aspera["speed"]} -P33001 '
                        f'-i {aspera["private_key"]} era-fasp@{fasp} ./'
                    ),
                    directory=outdir,
                    max_attempts=max_attempts,
                )

            fastq_md5 = md5sum(fastq)
            if fastq_md5 != md5:
                logging.log(STDOUT, f"MD5s, Observed: {fastq_md5}, Expected: {md5}")
                attempt += 1
                if os.path.exists(fastq):
                    os.remove(fastq)
                if attempt > max_attempts:
                    if not ftp_only:
                        ftp_only = True
                        attempt = 0
                    else:
                        logging.error(
                            f"Download failed after {max_attempts} attempts. "
                            "Please try again later or manually from SRA/ENA."
                        )
                        sys.exit(1)
                time.sleep(10)
            else:
                success = True

    return fastq


def merge_runs(runs, output):
    """Merge runs from an experiment."""
    if len(runs) > 1:
        run_fqs = " ".join(runs)
        execute(f"cat {run_fqs} > {output}")
        for p in runs:
            Path(p).unlink()
    else:
        Path(runs[0]).rename(output)


def get_run_info(query):
    """Retreive a list of unprocessed samples avalible from ENA."""
    url = f'{ENA_URL}&query="{query}"&fields={",".join(FIELDS)}'
    headers = {"Content-type": "application/x-www-form-urlencoded"}
    r = requests.get(url, headers=headers)
    if r.status_code == requests.codes.ok:
        data = []
        col_names = None
        for line in r.text.split("\n"):
            cols = line.rstrip().split("\t")
            if line:
                if col_names:
                    data.append(dict(zip(col_names, cols)))
                else:
                    col_names = cols
        return [True, data]
    else:
        return [False, [r.status_code, r.text]]


def write_json(data, output):
    """Write input data structure to a json file."""

    with open(output, "w") as fh:
        json.dump(data, fh, indent=4, sort_keys=True)


def parse_query(query, is_study, is_experiment, is_run):
    """Parse user query, to determine search field value."""
    if is_study:
        return f"study_accession={query}"
    elif is_experiment:
        return f"experiment_accession={query}"
    elif is_run:
        return f"run_accession={query}"
    else:
        # Try to guess...
        if query[1:3] == "RR":
            return f"run_accession={query}"
        elif query[1:3] == "RX":
            return f"experiment_accession={query}"
        else:
            return f"study_accession={query}"


def check_aspera(ascp, private_key, speed):
    """Verify Aspera Connect is available, not if it works."""
    error_message = None
    if not os.path.exists(ascp):
        error_message = f'cannot access "{ascp}": No such file or directory'
    else:
        if private_key:
            # User provided path to private key
            if not os.path.exists(private_key):
                error_message = (
                    f'cannot access "{private_key}": No such file or directory'
                )
        else:
            # Try to guess private key path, based on ascp path
            key_path = os.path.dirname(ascp).replace("/bin", "/etc")
            private_key = f"{key_path}/asperaweb_id_dsa.openssh"
            if not os.path.exists(private_key):
                error_message = (
                    f'cannot access "{private_key}": No such file or directory'
                )

    if error_message:
        logging.error(f"Aspera Related Error: {error_message}")
        sys.exit(1)
    else:
        return {"ascp": ascp, "private_key": private_key, "speed": speed}


def main():
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        conflict_handler="resolve",
        description=f"{PROGRAM} (v{VERSION}) - Download FASTQs from ENA or SRA",
    )
    group1 = parser.add_argument_group("Required Options", "")
    group1.add_argument(
        "query",
        metavar="ACCESSION",
        type=str,
        help="ENA/SRA accession to query. (Study, Experiment, or " "Run accession)",
    )
    group1.add_argument(
        "provider",
        choices=["sra", "ena"],
        type=str.lower,
        default="ena",
        nargs="?",
        help=(
            "Specify which provider (ENA or SRA) to use. Accepted Values: ENA SRA "
            "[default: %(default)s]"
        ),
    )

    group2 = parser.add_argument_group("Aspera Connect Options")
    group2.add_argument(
        "--aspera",
        metavar="STRING",
        type=str,
        help='Path to the Aspera Connect tool "ascp" (Default: "which ascp")',
    )
    group2.add_argument(
        "--aspera_key",
        metavar="STRING",
        type=str,
        help=(
            "Path to Aspera Connect private key, if not given, guess based on ascp path"
        ),
    )
    group2.add_argument(
        "--aspera_speed",
        metavar="STRING",
        type=str,
        default="100M",
        help="Speed at which Aspera Connect will download. [default: %(default)s]",
    )

    group3 = parser.add_argument_group("Query Related Options")
    group3.add_argument("--is_study", action="store_true", help="Query is a Study.")
    group3.add_argument(
        "--is_experiment", action="store_true", help="Query is an Experiment."
    )
    group3.add_argument("--is_run", action="store_true", help="Query is a Run.")
    group3.add_argument(
        "--group_by_experiment",
        action="store_true",
        help="Group Runs by experiment accession.",
    )
    group3.add_argument(
        "--group_by_sample", action="store_true", help="Group Runs by sample accession."
    )

    group4 = parser.add_argument_group("Helpful Options")
    group4.add_argument(
        "-o",
        "--outdir",
        metavar="OUTPUT_DIR",
        type=str,
        default="./",
        help="Directory to output downloads to. [default: %(default)s]",
    )
    group4.add_argument(
        "--prefix",
        metavar="PREFIX",
        type=str,
        default="fastq",
        help="Prefix to use for naming log files [default: %(default)s]",
    )
    group4.add_argument(
        "--max_attempts",
        metavar="INT",
        type=int,
        default=10,
        help="Maximum number of download attempts [default: %(default)d]",
    )
    group4.add_argument(
        "--cpus",
        metavar="INT",
        type=int,
        default=1,
        help="Total cpus used for downloading from SRA [default: %(default)d]",
    )
    group4.add_argument("--ftp_only", action="store_true", help="FTP only downloads.")
    group4.add_argument(
        "--sra_only",
        action="store_true",
        help=(
            "Do not attempt to fall back on ENA if SRA download does not work "
            "(e.g. missing FASTQ). [DEPRECATED - use --only-provider/-F]"
        ),
    )
    group4.add_argument(
        "-F",
        "--only-provider",
        action="store_true",
        help="Only attempt download from specified provider",
    )
    group4.add_argument(
        "--silent", action="store_true", help="Only critical errors will be printed."
    )
    group4.add_argument(
        "--verbose", action="store_true", help="Print debug related text."
    )
    group4.add_argument(
        "--debug",
        action="store_true",
        help="Skip downloads, print what will be downloaded.",
    )
    group4.add_argument("--version", action="version", version=f"{PROGRAM} {VERSION}")

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    # Setup logs
    FORMAT = "%(asctime)s:%(name)s:%(levelname)s - %(message)s"
    logging.basicConfig(
        format=FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger().setLevel(set_log_level(args.silent, args.verbose))

    aspera = (
        check_aspera(args.aspera, args.aspera_key, args.aspera_speed)
        if args.aspera
        else None
    )
    if not aspera:
        if args.provider == "ena":
            logging.info("Aspera Connect not available, using FTP for ENA downloads")
        args.ftp_only = True

    outdir = os.getcwd() if args.outdir == "./" else f"{args.outdir}"
    query = parse_query(args.query, args.is_study, args.is_experiment, args.is_run)

    # Start Download Process
    success, ena_data = get_run_info(query)
    if not success:
        logging.error("There was an issue querying ENA, exiting...")
        logging.error(f"STATUS: {ena_data[0]}")
        logging.error(f"TEXT: {ena_data[1]}")
        sys.exit(1)

    logging.info(f"Query: {args.query}")
    logging.info(f"Archive: {args.provider}")
    logging.info(f"Total Runs To Download: {len(ena_data)}")
    runs = {} if args.group_by_experiment or args.group_by_sample else None
    for i, run_info in enumerate(ena_data):
        run_acc = run_info["run_accession"]
        logging.info(f"\tWorking on run {run_acc}...")
        fastqs = None
        if args.provider == "ena":
            fastqs = ena_download(
                run_info,
                outdir,
                aspera=aspera,
                max_attempts=args.max_attempts,
                ftp_only=args.ftp_only,
            )

            if fastqs == ENA_FAILED:
                if args.only_provider:
                    logging.error(f"\tNo fastqs found in ENA for {run_acc}")
                    ena_data[i]["error"] = ENA_FAILED
                    fastqs = None
                else:
                    # Retry download from SRA
                    logging.info(f"\t{run_acc} not found on ENA, retrying from SRA")

                    fastqs = sra_download(
                        run_acc,
                        outdir,
                        cpus=args.cpus,
                        max_attempts=args.max_attempts,
                    )
                    if fastqs == SRA_FAILED:
                        logging.error(f"\t{run_acc} not found on SRA")
                        ena_data[i]["error"] = f"{ENA_FAILED}&{SRA_FAILED}"
                        fastqs = None

        else:
            fastqs = sra_download(
                run_acc,
                outdir,
                cpus=args.cpus,
                max_attempts=args.max_attempts,
            )
            if fastqs == SRA_FAILED:
                if args.sra_only or args.only_provider:
                    logging.error(f"\t{run_acc} not found on SRA")
                    ena_data[i]["error"] = SRA_FAILED
                    fastqs = None
                else:
                    # Retry download from ENA
                    logging.info(f"\t{run_acc} not found on SRA, retrying from ENA")
                    fastqs = ena_download(
                        run_info,
                        outdir,
                        aspera=aspera,
                        max_attempts=args.max_attempts,
                        ftp_only=args.ftp_only,
                    )
                    if fastqs == ENA_FAILED:
                        logging.error(f"\tNo fastqs found in ENA for {run_acc}")
                        ena_data[i]["error"] = f"{SRA_FAILED}&{ENA_FAILED}"
                        fastqs = None

        # Add the download results
        if fastqs:
            if args.group_by_experiment or args.group_by_sample:
                name = run_info["sample_accession"]
                if args.group_by_experiment:
                    name = run_info["experiment_accession"]

                if name not in runs:
                    runs[name] = {"r1": [], "r2": []}

                if fastqs["single_end"]:
                    runs[name]["r1"].append(fastqs["r1"])
                else:
                    runs[name]["r1"].append(fastqs["r1"])
                    runs[name]["r2"].append(fastqs["r2"])

    # If applicable, merge runs
    if runs and not args.debug:
        for name, vals in runs.items():
            if len(vals["r1"]) and len(vals["r2"]):
                # Not all runs labled as paired are actually paired.
                if len(vals["r1"]) == len(vals["r2"]):
                    logging.info(f"\tMerging paired end runs to {name}...")
                    merge_runs(vals["r1"], f"{outdir}/{name}_R1.fastq.gz")
                    merge_runs(vals["r2"], f"{outdir}/{name}_R2.fastq.gz")
                else:
                    logging.info("\tMerging single end runs to experiment...")
                    merge_runs(vals["r1"], f"{outdir}/{name}.fastq.gz")
            else:
                logging.info("\tMerging single end runs to experiment...")
                merge_runs(vals["r1"], f"{outdir}/{name}.fastq.gz")
        write_json(runs, f"{outdir}/{args.prefix}-run-mergers.json")
    write_json(ena_data, f"{outdir}/{args.prefix}-run-info.json")


if __name__ == "__main__":
    main()
