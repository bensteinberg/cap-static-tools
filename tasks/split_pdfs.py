import os
import zipfile
import io
from invoke import task
from pypdf import PdfReader, PdfWriter
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import json
import tempfile

from .helpers import (
    r2_s3_client as production_s3_client,
    get_volumes_metadata,
    R2_STATIC_BUCKET,
    R2_SPLIT_PDFS_BUCKET,
)

READ_BUCKET = R2_STATIC_BUCKET
WRITE_BUCKET = R2_SPLIT_PDFS_BUCKET


@task
def split_pdfs(ctx, reporter=None, volume=None, publication_year=None, s3_client=None):
    """Split PDFs into individual case files for all jurisdictions or a specific reporter."""
    print(
        f"Starting split_pdfs task for reporter: {reporter}, year: {publication_year}"
    )
    if s3_client is None:
        s3_client = production_s3_client

    volumes_to_process = get_volumes_to_process(reporter, volume, publication_year, s3_client)
    print(f"Volumes to process: {volumes_to_process}")

    total_volumes = len(volumes_to_process)
    print(f"Total volumes to process: {total_volumes}")

    with ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        futures = [
            executor.submit(process_volume, v, s3_client)
            for v in volumes_to_process
        ]

        for future in tqdm(
            as_completed(futures), total=total_volumes, desc="Processing Volumes"
        ):
            try:
                result = future.result()
                print(f"Processed volume result: {result}")
            except Exception as e:
                print(f"Error processing volume: {e}")

    print(f"Processed {total_volumes} volumes.")


def get_volumes_to_process(
    reporter=None, volume=None, publication_year=None, s3_client=None, r2_bucket=READ_BUCKET
):
    if volume and reporter is None:
        print("You have specified volume but no reporter. This is probably not what you want.")
        return []

    volumes_metadata = json.loads(get_volumes_metadata(r2_bucket))

    if reporter:
        volumes_metadata = [
            v for v in volumes_metadata if v["reporter_slug"] == reporter
        ]
    if volume:
        volumes_metadata = [
            v for v in volumes_metadata if v["volume_folder"] == volume
        ]
    if publication_year:
        volumes_metadata = [
            v
            for v in volumes_metadata
            if v.get("publication_year") == int(publication_year)
        ]

    return volumes_metadata


def get_cases_metadata(s3_client, bucket, volume):
    zip_key = f"{volume['reporter_slug']}/{volume['volume_folder']}.zip"
    unzipped_key = (
        f"{volume['reporter_slug']}/{volume['volume_folder']}/CasesMetadata.json"
    )

    try:
        # Try to get metadata from zip file first
        response = s3_client.get_object(Bucket=bucket, Key=zip_key)
        with zipfile.ZipFile(io.BytesIO(response["Body"].read())) as zip_ref:
            file_list = zip_ref.namelist()
            metadata_file_name = next(
                (name for name in file_list if name.endswith("CasesMetadata.json")),
                None,
            )
            if metadata_file_name:
                with zip_ref.open(metadata_file_name) as metadata_file:
                    return json.load(metadata_file)
            else:
                print(f"CasesMetadata.json not found in zip file {zip_key}")
    except s3_client.exceptions.NoSuchKey:
        # If zip file doesn't exist, try unzipped file
        try:
            response = s3_client.get_object(Bucket=bucket, Key=unzipped_key)
            return json.loads(response["Body"].read().decode("utf-8"))
        except Exception as e:
            print(f"Error getting cases metadata for {unzipped_key}: {str(e)}")
            return None
    except Exception as e:
        print(f"Error getting cases metadata from zip {zip_key}: {str(e)}")
        return None


def process_volume(volume, s3_client=production_s3_client):
    cases_metadata = get_cases_metadata(s3_client, READ_BUCKET, volume)

    if not cases_metadata:
        print(f"Skipping volume {volume['volume_folder']} due to missing metadata")
        return

    if not all([case["provenance"]["source"] == "Fastcase" for case in cases_metadata]):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
            pdf_path = temp_file.name
            download_pdf(volume, pdf_path, s3_client)

        try:
            case_pdfs = split_pdf(pdf_path, cases_metadata)
            print(f"Split {len(case_pdfs)} case PDFs")
            if len(case_pdfs):
                upload_case_pdfs(case_pdfs, volume, s3_client)
            return f"Processed {len(case_pdfs)} cases for volume {volume['volume_folder']}"
        except Exception as e:
            print(
                f"Error processing volume {volume['volume_folder']} of {volume['reporter_slug']}: {str(e)}"
            )
            return f"Error processing volume {volume['volume_folder']}: {str(e)}"
        finally:
            os.unlink(pdf_path)
    else:
        print(f"Skipping all-Fastcase volume {volume['volume_folder']}")
        return


def download_pdf(volume, local_path, s3_client=production_s3_client):
    key = f"{volume['reporter_slug']}/{volume['volume_folder']}.pdf"
    try:
        s3_client.download_file(READ_BUCKET, key, local_path)
    except Exception as e:
        print(
            f"Error downloading PDF for volume {volume['volume_folder']} of {volume['reporter_slug']}: {str(e)}"
        )
        raise


def split_pdf(pdf_path, cases_metadata):
    reader = PdfReader(pdf_path)

    case_pdfs = []
    for case in cases_metadata:
        if case["provenance"]["source"] != "Fastcase":
            writer = PdfWriter()
            start_page = case["first_page_order"] - 1
            end_page = case["last_page_order"]

            for page_num in range(start_page, end_page):
                writer.add_page(reader.pages[page_num])

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_case_file:
                writer.write(temp_case_file)
                case_pdfs.append((case["file_name"], temp_case_file.name))

    return case_pdfs


def upload_case_pdfs(case_pdfs, volume, s3_client=production_s3_client):
    for case_name, case_path in case_pdfs:
        key = f"{volume['reporter_slug']}/{volume['volume_folder']}/case-pdfs/{case_name}.pdf"
        try:
            s3_client.upload_file(case_path, WRITE_BUCKET, key)
            print(f"Uploaded {key} to {WRITE_BUCKET}")
        except Exception as e:
            print(
                f"Error uploading case PDF {case_name} for volume {volume['volume_folder']} of {volume['reporter_slug']}: {str(e)}"
            )
        finally:
            os.unlink(case_path)
