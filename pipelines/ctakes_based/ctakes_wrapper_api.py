# FILE: pipelines/ctakes_based/ctakes_wrapper_api.py
# (Complete Code - Includes subprocess output capturing and logging)

import asyncio
import logging
import os
import subprocess
import tempfile
import uuid
import time # Added for timing
import traceback # Added for exception formatting
from pathlib import Path
from typing import Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import PlainTextResponse, JSONResponse
from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---
CTAKES_HOME = os.getenv("CTAKES_HOME", "/opt/apache-ctakes-6.0.0")
CTAKES_LIB_DIR = Path(CTAKES_HOME, "lib")
CTAKES_RESOURCES_DIR = Path(CTAKES_HOME, "resources")
CTAKES_DESC_DIR = Path(CTAKES_HOME, "desc")
CTAKES_CONFIG_DIR = Path(CTAKES_HOME, "config")
PIPER_FILE_PATH = os.getenv("PIPER_FILE_PATH", "org/apache/ctakes/clinical/pipeline/CustomPipeline.piper")
IO_BASE_DIR = Path(os.getenv("CTAKES_IO_DIR", "/ctakes_io"))
INPUT_DIR = IO_BASE_DIR / "input"
OUTPUT_DIR = IO_BASE_DIR / "output"
HISTORY_BASE_DIR = Path(os.getenv("REQUEST_HISTORY_DIR", "/request_history")) # Base directory for storing request history
UMLS_API_KEY = os.getenv("UMLS_API_KEY")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("CTakesWrapperAPI")

# --- Determine cTAKES Execution Command & Validate Setup ---
CTAKES_EXECUTABLE_PATH = None
classpath = f"{CTAKES_RESOURCES_DIR}:{CTAKES_DESC_DIR}:{CTAKES_LIB_DIR}/*" # Classpath order might matter
piper_runner_class = "org.apache.ctakes.core.pipeline.PiperFileRunner"
java_executable = "java"
log4j_config_path = CTAKES_CONFIG_DIR / "log4j.xml"
startup_error_detail = None

if not Path(CTAKES_HOME).is_dir():
     startup_error_detail = f"cTAKES home directory not found at {CTAKES_HOME}."
elif not CTAKES_LIB_DIR.is_dir() or not CTAKES_RESOURCES_DIR.is_dir() or not CTAKES_DESC_DIR.is_dir():
    startup_error_detail = f"Cannot find essential cTAKES directories (lib, resources, desc) in {CTAKES_HOME}."
elif not log4j_config_path.is_file():
     logger.warning(f"Log4j configuration file not found at {log4j_config_path}. cTAKES logging may use defaults or be absent.")
     CTAKES_EXECUTABLE_PATH = "PiperFileRunner" # Assume PiperFileRunner even without log4j
else:
     CTAKES_EXECUTABLE_PATH = "PiperFileRunner" # Assume PiperFileRunner

if not CTAKES_EXECUTABLE_PATH and not startup_error_detail:
    startup_error_detail = "Failed to determine cTAKES execution method."

if not UMLS_API_KEY:
    logger.warning("Startup Warning: UMLS_API_KEY environment variable not set.")

if startup_error_detail:
     logger.error(f"STARTUP FAILED: {startup_error_detail}")

# Create I/O and History directories
try:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_BASE_DIR.mkdir(parents=True, exist_ok=True) # Create History directory
    logger.info(f"Ensured I/O directories exist: {INPUT_DIR}, {OUTPUT_DIR}")
    logger.info(f"Ensured History directory exists: {HISTORY_BASE_DIR}")
except OSError as e:
    err_msg = f"Could not create essential directories (I/O: {IO_BASE_DIR}, History: {HISTORY_BASE_DIR}): {e}"
    logger.error(f"FATAL: {err_msg}")
    if not startup_error_detail:
        startup_error_detail = err_msg


app = FastAPI(title="cTAKES Internal Wrapper API", version="1.0.0")
ctakes_lock = asyncio.Lock() # Lock to ensure sequential processing

@app.post("/process", response_class=PlainTextResponse)
async def process_text(text: str = Body(..., media_type='text/plain')):
    """ Processes text using file-based cTAKES and saves input/output/logs to a timestamped history folder. """
    run_id = str(uuid.uuid4())
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S") # Format: YYYYMMDD_HHMMSS
    history_folder_name = f"{timestamp_str}_{run_id}" # New folder name format
    log_prefix = f"[Run {history_folder_name}]" # Use new format in logs for correlation

    input_file: Optional[Path] = None
    output_dir_run: Optional[Path] = None
    output_file: Optional[Path] = None
    history_run_dir: Optional[Path] = None # For storing request history

    # Check for startup errors first
    if startup_error_detail:
        raise HTTPException(status_code=503, detail=f"Service Unavailable: {startup_error_detail}")
    # Check runtime prerequisites
    if not text.strip():
        raise HTTPException(status_code=400, detail="Input text cannot be empty.")
    if not UMLS_API_KEY:
        raise HTTPException(status_code=500, detail="Server configuration error: Missing UMLS API Key.")

    # --- History Saving Setup ---
    try:
        history_run_dir = HISTORY_BASE_DIR / history_folder_name # Use the new folder name
        history_run_dir.mkdir(parents=True, exist_ok=True) # Create specific dir for this run
        logger.info(f"{log_prefix} Created history directory: {history_run_dir}")

        # Save input text to history
        input_history_file = history_run_dir / "input.txt"
        input_history_file.write_text(text, encoding="utf-8")
        logger.info(f"{log_prefix} Saved input text to: {input_history_file}")
    except OSError as e:
        logger.warning(f"{log_prefix} Failed to create history directory or save input: {e}. Processing will continue.")
        history_run_dir = None # Disable output/log saving if input saving failed
    except Exception as e_hist:
        logger.warning(f"{log_prefix} Unexpected error saving input history: {e_hist}. Processing will continue.")
        history_run_dir = None # Disable output/log saving
    # --- End History Saving Setup ---

    async with ctakes_lock:
        try:
            logger.info(f"{log_prefix} Processing request.")
            # 1. Create temporary input file
            temp_input_filename = f"{history_folder_name}.txt" # Use unique name
            # Use NamedTemporaryFile to ensure unique name even with same timestamp
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=INPUT_DIR, delete=False, suffix=".txt", prefix=f"{timestamp_str}_") as f:
                input_file = Path(f.name)
                f.write(text)
            logger.info(f"{log_prefix} Temporary input file: {input_file}")

            # 2. Prepare temporary output directory and expected output file path
            output_dir_run = OUTPUT_DIR / history_folder_name
            output_dir_run.mkdir(exist_ok=True)
            output_file = output_dir_run / f"{input_file.name}.xmi"
            logger.info(f"{log_prefix} Expecting temporary output: {output_file}")

            # 3. Construct cTAKES command
            umls_key_arg = "--key"
            base_cmd = [java_executable, "-cp", classpath]
            if log4j_config_path.is_file():
                base_cmd.append(f"-Dlog4j.configuration=file:{log4j_config_path}")
            else:
                 logger.warning(f"log4j.xml not found at {log4j_config_path}, proceeding without explicit log config for run {history_folder_name}.")

            cmd = base_cmd + [
                "-Xms512m", "-Xmx4g", # Adjust memory as needed
                piper_runner_class,
                "-p", PIPER_FILE_PATH,
                "-i", str(input_file.parent), # Input *Directory*
                "--xmiOut", str(output_dir_run), # Output *Directory*
                umls_key_arg, UMLS_API_KEY,
            ]

            if CTAKES_EXECUTABLE_PATH != "PiperFileRunner":
                 raise HTTPException(status_code=500, detail="Internal Server Error: cTAKES execution method misconfigured.")

            logger.info(f"{log_prefix} Executing command: {' '.join(cmd)}")
            start_time = time.time()

            # 4. Run cTAKES subprocess and capture output
            try:
                # --- MODIFIED SUBPROCESS CALL ---
                process = await asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    check=False, # Don't raise exception on non-zero exit, we'll check returncode
                    capture_output=True, # Capture stdout and stderr
                    text=True, # Decode stdout/stderr as text
                    encoding='utf-8', # Specify encoding
                    errors='replace' # Handle potential decoding errors
                )
                # --- END MODIFICATION ---

                end_time = time.time()
                duration = end_time - start_time
                logger.info(f"{log_prefix} Subprocess finished in {duration:.2f} seconds with return code: {process.returncode}")

                # --- ADD LOGGING FOR STDOUT/STDERR ---
                stdout = process.stdout or "(empty)"
                stderr = process.stderr or "(empty)"

                logger.debug(f"{log_prefix} cTAKES stdout (last 1000 chars):\n...{stdout[-1000:]}")
                logger.debug(f"{log_prefix} cTAKES stderr (last 2000 chars):\n...{stderr[-2000:]}")

                # Save full stdout/stderr to history if possible
                if history_run_dir:
                    try:
                        with open(history_run_dir / "ctakes_stdout.log", "w", encoding='utf-8') as f_stdout:
                            f_stdout.write(stdout)
                        with open(history_run_dir / "ctakes_stderr.log", "w", encoding='utf-8') as f_stderr:
                            f_stderr.write(stderr)
                        logger.info(f"{log_prefix} Saved full stdout/stderr to history directory.")
                    except Exception as e_hist_log:
                        logger.warning(f"{log_prefix} Failed to save stdout/stderr to history: {e_hist_log}")
                # --- END LOGGING ADDITION ---

                # 5. Check return code AFTER logging output
                if process.returncode != 0:
                    error_message = f"cTAKES execution failed with return code {process.returncode}."
                    logger.error(f"{log_prefix} {error_message} See ctakes_stderr.log in history for details.")
                    # Save error details also to a specific file if history dir exists
                    if history_run_dir:
                        try:
                            with open(history_run_dir / "error.log", "w", encoding='utf-8') as f_err:
                                f_err.write(f"{error_message}\n\nSTDERR:\n{stderr}")
                        except Exception as e_hist_err:
                             logger.warning(f"{log_prefix} Failed to save error details to history: {e_hist_err}")
                    # Raise HTTPException for FastAPI
                    raise HTTPException(status_code=500, detail=f"{error_message} Stderr (start): {stderr[:500]}...")

                logger.info(f"{log_prefix} cTAKES execution successful.")

            except FileNotFoundError:
                logger.exception(f"{log_prefix} Error: 'java' command not found. Is Java installed and in PATH?")
                raise HTTPException(status_code=500, detail="'java' command not found on server.")
            except Exception as e_subproc:
                logger.exception(f"{log_prefix} An unexpected error occurred during cTAKES subprocess execution: {e_subproc}")
                # Save exception details if history dir exists
                if history_run_dir:
                    try:
                        with open(history_run_dir / "error.log", "w", encoding='utf-8') as f_err:
                            f_err.write(f"Python Exception during subprocess execution:\n{traceback.format_exc()}")
                    except Exception as e_hist_py_err:
                        logger.warning(f"{log_prefix} Failed to save Python exception details to history: {e_hist_py_err}")
                # Re-raise if it's already an HTTPException, otherwise wrap it
                if isinstance(e_subproc, HTTPException):
                    raise
                else:
                    raise HTTPException(status_code=500, detail=f"Internal server error during cTAKES execution: {e_subproc}")

            # 6. Read output file
            if output_file.is_file():
                logger.info(f"{log_prefix} Reading temporary output file: {output_file}")
                try:
                    xmi_content = output_file.read_text(encoding="utf-8")
                    logger.info(f"{log_prefix} Read {len(xmi_content)} bytes from temporary XMI.")

                    # --- Save Output to History ---
                    if history_run_dir: # Check if history dir creation was successful
                        try:
                            output_history_file = history_run_dir / "output.xmi"
                            output_history_file.write_text(xmi_content, encoding="utf-8")
                            logger.info(f"{log_prefix} Saved output XMI to: {output_history_file}")
                        except Exception as e_hist_out:
                            logger.warning(f"{log_prefix} Failed to save output XMI to history ({history_run_dir}): {e_hist_out}")
                    # --- End Save Output to History ---

                    return PlainTextResponse(content=xmi_content, media_type="application/xml")
                except Exception as read_err:
                     logger.exception(f"{log_prefix} Error reading temporary output file {output_file} or saving history:")
                     raise HTTPException(status_code=500, detail=f"Failed to read cTAKES output file or save history: {read_err}")
            else:
                logger.error(f"{log_prefix} Expected temporary output file NOT FOUND: {output_file}")
                files_in_output = list(output_dir_run.glob('*')) if output_dir_run.exists() else []
                logger.error(f"{log_prefix} Files found in temporary output dir ({output_dir_run}): {files_in_output}")
                logger.error(f"{log_prefix} cTAKES stderr (for clues):\n{stderr}") # Log stderr again if output missing
                raise HTTPException(status_code=500, detail=f"cTAKES completed but temporary output file not found: {output_file.name}")

        except HTTPException: raise # Re-raise HTTPExceptions directly
        except Exception as e:
            logger.exception(f"{log_prefix} Unexpected error during processing:")
            # Save general exception details if history dir exists
            if history_run_dir:
                try:
                    with open(history_run_dir / "error.log", "w", encoding='utf-8') as f_err:
                         f_err.write(f"General Python Exception during processing:\n{traceback.format_exc()}")
                except Exception as e_hist_gen_err:
                    logger.warning(f"{log_prefix} Failed to save general exception details to history: {e_hist_gen_err}")
            raise HTTPException(status_code=500, detail=f"Internal server error in wrapper: {e}")
        finally:
            # 7. Cleanup temporary files reliably (History files are NOT cleaned)
            if input_file and input_file.exists():
                try: input_file.unlink(); logger.debug(f"{log_prefix} Cleaned temp input: {input_file}")
                except OSError as e_unlink: logger.warning(f"{log_prefix} Failed cleanup (temp input {input_file}): {e_unlink}")
            # Cleanup the temporary output directory and its contents
            if output_dir_run and output_dir_run.exists():
                cleaned_files = []
                failed_files = []
                for item in output_dir_run.glob('*'): # Delete files inside first
                    try:
                        if item.is_file():
                            item.unlink()
                            cleaned_files.append(item.name)
                    except OSError as e_unlink_out:
                        logger.warning(f"{log_prefix} Failed cleanup (temp output file {item}): {e_unlink_out}")
                        failed_files.append(item.name)

                if not failed_files: # Only remove dir if all files inside were removed
                     try:
                        output_dir_run.rmdir(); logger.debug(f"{log_prefix} Cleaned temp output dir: {output_dir_run} (contained: {cleaned_files})")
                     except OSError as e_rmdir:
                        logger.warning(f"{log_prefix} Failed cleanup (temp output dir {output_dir_run}): {e_rmdir}")
                else:
                     logger.warning(f"{log_prefix} Did not remove temp output dir {output_dir_run} due to file cleanup failures: {failed_files}")
            # Lock released automatically

@app.get("/health")
async def health_check():
    """ Basic health check endpoint for the wrapper service. """
    status_code = 200
    status = "OK"
    detail_items = ["cTAKES wrapper prerequisites seem met."]
    issues = []

    if startup_error_detail:
         status = "ERROR"
         issues.append(startup_error_detail)
         status_code = 503 # Service Unavailable
    if not UMLS_API_KEY:
         if status != "ERROR": status = "WARNING"
         issues.append("Missing UMLS_API_KEY environment variable. UMLS lookups may fail.")
    if status != "ERROR":
        try:
            # Test writability of history base directory
            test_file = HISTORY_BASE_DIR / f".healthcheck_{uuid.uuid4()}"
            test_file.touch()
            test_file.unlink()
        except Exception as e:
            if status != "ERROR": status = "WARNING"
            issues.append(f"History directory ({HISTORY_BASE_DIR}) might not be writable: {e}")

    if issues:
        detail = " Issues found: " + " | ".join(issues)
    else:
        detail = detail_items[0] # Default OK message


    return JSONResponse(
        status_code=status_code,
        content={
            "status": status,
            "detail": detail,
            "ctakes_home_configured": CTAKES_HOME,
            "history_directory_configured": str(HISTORY_BASE_DIR)
            }
    )

# ### END OF FILE ###