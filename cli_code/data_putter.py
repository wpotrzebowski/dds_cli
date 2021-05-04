"""Data putter."""

###############################################################################
# IMPORTS ########################################################### IMPORTS #
###############################################################################

# Standard library
import logging
import pathlib
import os

# Installed
import botocore
import boto3
import requests
import rich
from rich.progress import Progress, SpinnerColumn
import simplejson

# Own modules
from cli_code import base
from cli_code import file_handler_local as fhl
from cli_code import s3_connector as s3
from cli_code import DDSEndpoint
from cli_code.cli_decorators import (
    verify_proceed,
    update_status,
    subpath_required,
)
from cli_code import status
from cli_code import text_handler as txt
from cli_code import file_encryptor as fe
from cli_code import file_compressor as fc
from cli_code import data_remover as dr

###############################################################################
# START LOGGING CONFIG ################################# START LOGGING CONFIG #
###############################################################################

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG)

###############################################################################
# RICH CONFIG ################################################### RICH CONFIG #
###############################################################################

console = rich.console.Console()

###############################################################################
# CLASSES ########################################################### CLASSES #
###############################################################################


class DataPutter(base.DDSBaseClass):
    """Data putter class."""

    def __init__(
        self,
        temporary_destination: dict,
        username: str = None,
        config: pathlib.Path = None,
        project: str = None,
        break_on_fail: bool = False,
        overwrite: bool = False,
        source: tuple = (),
        source_path_file: pathlib.Path = None,
        silent: bool = False,
    ):

        # Initiate DDSBaseClass to authenticate user
        super().__init__(username=username, config=config, project=project)

        # Initiate DataPutter specific attributes
        self.break_on_fail = break_on_fail
        self.overwrite = overwrite
        self.silent = silent
        self.filehandler = None
        self.log_location = temporary_destination["LOGS"]

        # Only method "put" can use the DataPutter class
        if self.method != "put":
            console.print("\n:no_entry_sign: " f"Unauthorized method: {self.method} " ":no_entry_sign:\n")
            os._exit(0)

        # Start file prep progress
        with Progress(
            "[bold]{task.description}",
            SpinnerColumn(spinner_name="dots12", style="white"),
        ) as progress:
            # Spinner while collecting file info
            wait_task = progress.add_task("Collecting and preparing data", step="prepare")

            # Get file info
            self.filehandler = fhl.LocalFileHandler(
                user_input=(source, source_path_file),
                temporary_destination=temporary_destination["FILES"],
            )

            # Verify that the Safespring S3 bucket exists
            self.verify_bucket_exist()

            # Check which, if any, files exist in the db
            files_in_db = self.filehandler.check_previous_upload(token=self.token)

            # Quit if error and flag
            if files_in_db and self.break_on_fail and not self.overwrite:
                # TODO (ina): Fix better print out
                console.print(
                    "Some files have already been uploaded (or have identical names to "
                    "previously uploaded files) and the '--break-on-fail' flag used. "
                    "Use '--overwrite' if you want to upload these files again."
                )
                os._exit(0)

            # Generate status dict
            self.status = self.filehandler.create_upload_status_dict(
                existing_files=files_in_db, overwrite=self.overwrite
            )

            # Remove spinner
            progress.remove_task(wait_task)

        if not self.filehandler.data:
            console.print("No data to upload.")
            os._exit(0)

    # Public methods ###################### Public methods #
    @verify_proceed
    @subpath_required
    def protect_and_upload(self, file, progress):
        """Processes and uploads the file while handling the progress bars."""

        # Variables
        all_ok, saved, message = (False, False, "")  # Error catching
        file_info = self.filehandler.data[file]  # Info on current file
        file_public_key, salt = ("", "")  # Crypto info

        # Progress bar for processing
        task = progress.add_task(
            description=txt.TextHandler.task_name(file=file, step="encrypt"),
            total=file_info["size_raw"],
            visible=not self.silent,
        )

        # Stream chunks from file
        # stream_function = (
        #     self.filehandler.read_file
        #     if file_info["compressed"]
        #     else fc.Compressor.compress_file
        # )
        streamed_chunks = self.filehandler.stream_from_file(file=file)
        # streamed_chunks = stream_function(
        #     file=file_info["path_raw"],
        # )

        # Stream the chunks into the encryptor to save the encrypted chunks
        with fe.Encryptor(project_keys=self.keys) as encryptor:

            # Encrypt and save chunks
            saved, message = encryptor.encrypt_filechunks(
                chunks=streamed_chunks,
                outfile=file_info["path_processed"],
                progress=(progress, task),
            )

            # Get hex version of public key -- saved in db
            self.filehandler.data[file]["public_key"] = encryptor.get_public_component_hex(
                private_key=encryptor.my_private
            )
            self.filehandler.data[file]["key_salt"] = encryptor.salt

        LOG.debug("Updating file processed size: %s", file_info["path_processed"])
        # Update file size
        self.filehandler.data[file]["size_processed"] = file_info["path_processed"].stat().st_size

        if saved:
            LOG.info(
                "File successfully encrypted: %s. New location: %s",
                file,
                file_info["path_processed"],
            )
            # Update progress bar for upload
            progress.reset(
                task,
                description=txt.TextHandler.task_name(file=file, step="put"),
                total=self.filehandler.data[file]["size_processed"],
                step="put",
            )

            # Perform upload
            file_uploaded, message = self.put(file=file, progress=progress, task=task)

            LOG.debug("File uploaded: %s", file_uploaded)
            # Perform db update
            if file_uploaded:
                db_updated, message = self.add_file_db(file=file)

                if db_updated:
                    all_ok = True

        if not saved or all_ok:
            # Delete temporary processed file locally
            LOG.debug(
                "Deleting file %s - exists: %s",
                file_info["path_processed"],
                file_info["path_processed"].exists(),
            )
            dr.DataRemover.delete_tempfile(file=file_info["path_processed"])

        # Remove progress bar task
        progress.remove_task(task)

        return all_ok, message

    @update_status
    def put(self, file, progress, task):
        """Uploads files to the cloud."""

        # Variables
        uploaded = False
        error = ""

        # File info
        file_local = str(self.filehandler.data[file]["path_processed"])
        file_remote = self.filehandler.data[file]["path_remote"]

        with s3.S3Connector(project_id=self.project, token=self.token) as conn:

            # Check that connection ok and upload file
            if None in [
                conn.safespring_project,
                conn.url,
                conn.keys,
                conn.bucketname,
            ]:
                error = "No s3 info returned! " + conn.message
            else:
                # Upload file
                try:
                    conn.resource.meta.client.upload_file(
                        Filename=file_local,
                        Bucket=conn.bucketname,
                        Key=file_remote,
                        ExtraArgs={
                            "ACL": "private",  # Access control list
                            "CacheControl": "no-store",  # Don't store cache
                        },
                        Callback=status.ProgressPercentage(
                            progress=progress,
                            task=task,
                        )
                        if task is not None
                        else None,
                    )
                except (
                    botocore.client.ClientError,
                    boto3.exceptions.Boto3Error,
                    FileNotFoundError,
                    TypeError,
                ) as err:
                    error = f"S3 upload of file '{file}' failed: {err}"
                    LOG.exception("%s: %s", file, err)
                else:
                    uploaded = True

        return uploaded, error

    @update_status
    def add_file_db(self, file):
        """Make API request to add file to DB."""

        # Variables
        added_to_db = False
        error = ""

        # Get file info and specify info required in db
        fileinfo = self.filehandler.data[file]
        params = {
            "name": file,
            "name_in_bucket": fileinfo["path_remote"],
            "subpath": fileinfo["subpath"],
            "size": fileinfo["size_raw"],
            "size_processed": fileinfo["size_processed"],
            "compressed": not fileinfo["compressed"],
            "salt": fileinfo["key_salt"],
            "public_key": fileinfo["public_key"],
            "checksum": fileinfo["checksum"],
        }

        # Send file info to API - post if new file, put if overwrite
        put_or_post = requests.put if fileinfo["overwrite"] else requests.post
        try:
            response = put_or_post(
                DDSEndpoint.FILE_NEW,
                params=params,
                headers=self.token,
                timeout=DDSEndpoint.TIMEOUT,
            )
        except requests.exceptions.RequestException as err:
            error = str(err)
            LOG.warning(error)
        else:
            # Error if failed
            if not response.ok:
                error = f"Failed to add file '{file}' to database: {response.text}"
                LOG.exception(error)
                return added_to_db, error

            try:
                added_to_db, error = (True, response.json().get("message"))
            except simplejson.JSONDecodeError as err:
                error = str(err)
                LOG.warning(error)

        return added_to_db, error
