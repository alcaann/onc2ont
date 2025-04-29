# FILE: api/main.py
# (Replace the content of your existing file with this)

import asyncio
import json
import logging
import os
import importlib # <--- Added for dynamic loading
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketState
from typing import Optional, List, Dict, Any
from collections.abc import Callable
from dotenv import load_dotenv # <--- Added to load .env file

# --- Pipeline Imports ---
# Import the base class for type checking
try:
    from pipelines.base_pipeline import BaseProcessingPipeline
except ImportError as e:
    logging.basicConfig(level=logging.ERROR) # Basic config if logging not set up yet
    logging.error(f"CRITICAL: Failed to import BaseProcessingPipeline: {e}", exc_info=True)
    # Define a dummy class to allow app to potentially start and report error,
    # but pipeline loading will fail later.
    class BaseProcessingPipeline: pass


# --- Load Environment Variables ---
load_dotenv() # Load variables from .env file into environment

# --- Logging Setup ---
log_level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)
logging.basicConfig(level=log_level, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__) # Logger specific to this API file


# --- Global Variables ---
processing_pipeline: Optional[BaseProcessingPipeline] = None
pipeline_error: Optional[str] = None # Store loading errors

# --- Pipeline Loading Logic ---
def load_pipeline():
    """Dynamically loads and instantiates the processing pipeline specified in env vars."""
    global processing_pipeline, pipeline_error
    # Reset state
    processing_pipeline = None
    pipeline_error = None

    pipeline_class_path = os.getenv("PROCESSING_PIPELINE")

    if not pipeline_class_path:
        pipeline_error = "PROCESSING_PIPELINE environment variable not set."
        logger.critical(pipeline_error)
        return

    logger.info(f"Attempting to load pipeline from path: {pipeline_class_path}")
    try:
        # Dynamically import the module and get the class
        module_path, class_name = pipeline_class_path.rsplit('.', 1)
        PipelineModule = importlib.import_module(module_path)
        PipelineClass = getattr(PipelineModule, class_name)

        # Verify it's a subclass of our base pipeline interface
        if not issubclass(PipelineClass, BaseProcessingPipeline):
             pipeline_error = f"Class '{class_name}' in '{module_path}' does not inherit from BaseProcessingPipeline."
             logger.critical(pipeline_error)
             return

        # Instantiate the pipeline
        processing_pipeline = PipelineClass() # Calls the __init__ of the specific pipeline class
        logger.info(f"Successfully loaded and instantiated pipeline: {pipeline_class_path}")

        # Optional: Add pipeline-specific checks after instantiation if needed
        # e.g., check if cTAKES URL is reachable for CTakesPipeline, or if spaCy model loaded.
        # Example check (adapt based on actual pipeline):
        # if isinstance(processing_pipeline, SpacyRuleBasedPipeline) and not getattr(processing_pipeline, 'nlp', None):
        #     pipeline_error = f"Pipeline {class_name} initialized but failed its internal check (e.g., NLP model load)."
        #     logger.critical(pipeline_error)
        #     processing_pipeline = None # Reset if initialization failed post-check

    except ModuleNotFoundError:
        pipeline_error = f"Pipeline module not found: {module_path}"
        logger.critical(pipeline_error, exc_info=True)
    except AttributeError:
        pipeline_error = f"Pipeline class not found: {class_name} in {module_path}"
        logger.critical(pipeline_error, exc_info=True)
    except ImportError as e:
        # Catch errors if pipeline dependencies are missing (e.g., spacy model, db driver)
        pipeline_error = f"Import error loading pipeline {pipeline_class_path}: {e}"
        logger.critical(pipeline_error, exc_info=True)
    except Exception as e:
        # Catch-all for errors during pipeline instantiation (e.g., __init__ failure in the pipeline class)
        pipeline_error = f"Error instantiating pipeline {pipeline_class_path}: {e}"
        logger.exception("Pipeline instantiation failed") # Log full traceback


# --- FastAPI App Setup ---
app = FastAPI(title="Oncology Phrase Processor", version="1.0.0")

@app.on_event("startup")
async def startup_event():
    logger.info("Application startup initiated...")
    # Attempt to load the configured pipeline on startup
    load_pipeline()
    if processing_pipeline:
        logger.info("Processing pipeline loaded successfully during startup.")
    else:
        logger.error(f"CRITICAL: Processing pipeline failed to load during startup. Error: {pipeline_error}")
    logger.info("Application startup complete.")


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Application shutdown.")
    # Add any cleanup logic if needed (e.g., close DB pool if used globally)


# --- WebSocket Endpoint ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handles WebSocket connections for phrase processing and results."""

    # --- Check if pipeline is available before accepting ---
    if processing_pipeline is None:
        logger.error("Pipeline not available, rejecting WebSocket connection.")
        # Immediately close the connection attempt without accepting
        await websocket.close(code=1011, reason=f"Server Error: Processing pipeline failed to load ({pipeline_error or 'Unknown reason'}).")
        return

    # Pipeline is available, accept the connection
    await websocket.accept()
    logger.info(f"WebSocket connection accepted from: {websocket.client.host}:{websocket.client.port}")

    log_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    log_sender_task: Optional[asyncio.Task] = None
    processing_task: Optional[asyncio.Task] = None # Task for the main processing
    owl_gen_task: Optional[asyncio.Task] = None # Task for OWL generation
    main_event_loop = asyncio.get_running_loop()

    async def send_logs():
        """Task to continuously send logs from the queue to the client."""
        while True:
            try:
                message = await log_queue.get()
                if message is None: # Sentinel value to stop the task
                    log_queue.task_done(); break
                if websocket.client_state == WebSocketState.CONNECTED:
                    # Send log message as JSON object
                    log_payload = json.dumps({"type": "log", "payload": message})
                    await websocket.send_text(log_payload)
                else:
                    logger.warning("WebSocket disconnected while trying to send log.")
                    log_queue.task_done(); break # Exit if websocket closed
                log_queue.task_done()
            except asyncio.CancelledError:
                 logger.info("Log sending task cancelled.")
                 # Ensure task_done is called even on cancellation if item was retrieved
                 try: log_queue.task_done()
                 except ValueError: pass
                 break
            except Exception as e:
                 logger.error(f"Error in send_logs task: {e}", exc_info=True)
                 try: log_queue.task_done() # Mark done to avoid hangs
                 except ValueError: pass
                 break # Stop the task on error

    def log_callback_sync(message: str):
        """Thread-safe callback to put logs onto the async queue."""
        try:
            # Use the captured main_event_loop for thread safety from sync code
            main_event_loop.call_soon_threadsafe(log_queue.put_nowait, message)
        except Exception as e:
            # Log error if putting to queue fails (e.g., queue full, loop stopped)
            logger.error(f"Error submitting log message to queue from thread: {e}")

    try:
        while True:
            # Wait for messages from the client
            data = await websocket.receive_text()
            logger.debug(f"Received raw message via WS: {data[:200]}...") # Log received data carefully

            try:
                message_data = json.loads(data)
                msg_type = message_data.get("type")
                payload = message_data.get("payload")

                if msg_type == "process_phrase" and isinstance(payload, str):
                    phrase = payload
                    logger.info(f"Processing request for phrase: '{phrase[:100]}...' using {processing_pipeline.__class__.__name__}")

                    # --- Cancel any previous ongoing tasks for this connection ---
                    if processing_task and not processing_task.done():
                        logger.warning("New request received. Cancelling previous processing task.")
                        processing_task.cancel()
                    if owl_gen_task and not owl_gen_task.done():
                        logger.warning("New request received. Cancelling previous OWL generation task.")
                        owl_gen_task.cancel()
                    if log_sender_task and not log_sender_task.done():
                        logger.debug("Cancelling previous log sender task for new request.")
                        log_sender_task.cancel()
                        # Ensure sentinel is put to allow clean exit if needed, handle QueueFull
                        try: log_queue.put_nowait(None)
                        except asyncio.QueueFull: pass

                    # Clear the log queue for the new request
                    while not log_queue.empty():
                         try: log_queue.get_nowait(); log_queue.task_done()
                         except (asyncio.QueueEmpty, ValueError): break

                    # --- Start new tasks for this request ---
                    log_sender_task = asyncio.create_task(send_logs(), name="LogSenderTask")
                    await websocket.send_json({"type": "status", "payload": "Processing started..."})

                    # --- Execute the pipeline's process method in a thread ---
                    processing_task = asyncio.create_task(
                        asyncio.to_thread(
                            processing_pipeline.process, # Call the 'process' method of the INSTANCE
                            phrase,
                            log_func=log_callback_sync # Pass the thread-safe callback
                        ),
                        name="PipelineProcessTask"
                    )

                    try:
                        # Wait for the processing task to complete
                        results: Dict[str, List[Dict[str, Any]]] = await processing_task

                        # Check if task was cancelled before sending result
                        if processing_task.cancelled():
                             logger.info("Processing task was cancelled before completion.")
                             await websocket.send_json({"type": "status", "payload": "Processing cancelled."})
                             continue # Wait for next message

                        logger.info(f"Processing completed. Found {len(results.get('concepts',[]))} concepts, {len(results.get('relations',[]))} relations.")
                        # Send the main JSON result
                        await websocket.send_json({"type": "result", "payload": results})
                        logger.debug("Sent final JSON results via WS.")

                        # --- Trigger OWL Generation (in thread) ---
                        await websocket.send_json({"type": "status", "payload": "Generating OWL..."})
                        try:
                            # Import here to attempt loading only when needed
                            from scripts.owl_generator import generate_owl
                            concepts = results.get('concepts', [])
                            relations = results.get('relations', [])

                            owl_gen_task = asyncio.create_task(
                                asyncio.to_thread(generate_owl, concepts, relations),
                                name="OwlGenerationTask"
                            )
                            owl_output_string: str = await owl_gen_task

                            if owl_gen_task.cancelled():
                                 logger.info("OWL generation task was cancelled.")
                                 await websocket.send_json({"type": "status", "payload": "OWL generation cancelled."})
                            else:
                                 await websocket.send_json({"type": "owl_result", "payload": owl_output_string})
                                 logger.info("OWL generation successful, sent to client.")
                                 await websocket.send_json({"type": "status", "payload": "Processing complete."})

                        except ImportError:
                             logger.error("scripts.owl_generator module or generate_owl function not found.")
                             await websocket.send_json({"type": "error", "payload": "Server Error: OWL generation component not available."})
                             await websocket.send_json({"type": "status", "payload": "Processing complete (OWL failed)."})
                        except asyncio.CancelledError:
                            logger.info("OWL generation task cancelled.")
                            await websocket.send_json({"type": "status", "payload": "OWL generation cancelled."})
                        except Exception as owl_err:
                             logger.exception("Error during OWL generation:")
                             await websocket.send_json({"type": "error", "payload": f"Server Error: Could not generate OWL output ({owl_err})."})
                             await websocket.send_json({"type": "status", "payload": "Processing complete (OWL failed)."})

                    except asyncio.CancelledError:
                         logger.info("Processing task cancelled (likely by new request or disconnect).")
                         # Status might have already been sent, or connection might be closing
                    except Exception as e:
                        # Catch errors *from within* the processing_pipeline.process call (raised in the thread)
                        logger.exception(f"Error during pipeline execution for '{phrase[:50]}...':")
                        if websocket.client_state == WebSocketState.CONNECTED:
                            error_payload = json.dumps({"type": "error", "payload": f"Processing failed: {e}"})
                            await websocket.send_text(error_payload)
                    finally:
                        # Signal and wait for log sender task to finish for this request
                        if log_sender_task and not log_sender_task.done():
                            logger.debug("Signalling log sender task to stop after request completion.")
                            try:
                                # Put sentinel only if task wasn't cancelled earlier
                                if not log_sender_task.cancelled():
                                    await log_queue.put(None) # Send sentinel
                                # Wait briefly for logs to flush
                                await asyncio.wait_for(log_sender_task, timeout=5.0)
                                logger.debug("Log sender task finished after request.")
                            except (asyncio.TimeoutError, asyncio.CancelledError):
                                logger.warning("Log sender task timed out or was cancelled while waiting.")
                                if not log_sender_task.done(): log_sender_task.cancel() # Force cancel if stuck
                            except asyncio.QueueFull:
                                logger.warning("Log queue was full when trying to send sentinel.")
                                if not log_sender_task.done(): log_sender_task.cancel()
                            except Exception as e_log_wait:
                                logger.error(f"Error waiting for log sender task: {e_log_wait}")
                                if not log_sender_task.done(): log_sender_task.cancel()

                else:
                     # Handle invalid message type or payload format
                     logger.warning(f"Received invalid message type/payload via WS: Type='{msg_type}', Payload Type='{type(payload)}'")
                     if websocket.client_state == WebSocketState.CONNECTED:
                          error_payload = json.dumps({"type": "error", "payload": "Invalid message format. Send {'type': 'process_phrase', 'payload': 'your text'}"})
                          await websocket.send_text(error_payload)

            except json.JSONDecodeError:
                 # Handle non-JSON message
                 logger.warning(f"Received non-JSON message via WS: {data[:200]}")
                 if websocket.client_state == WebSocketState.CONNECTED:
                    error_payload = json.dumps({"type": "error", "payload": "Invalid JSON message received."})
                    await websocket.send_text(error_payload)
            except WebSocketDisconnect:
                 # Handle disconnection during message processing
                 logger.info(f"WebSocket disconnected during message processing: {websocket.client.host}:{websocket.client.port}")
                 break # Exit the inner loop
            except Exception as e:
                 # Handle other unexpected errors during message handling loop
                 logger.error(f"Unexpected error handling WebSocket message: {e}", exc_info=True)
                 if websocket.client_state == WebSocketState.CONNECTED:
                     try:
                          error_payload = json.dumps({"type": "error", "payload": f"Internal server error: {e}"})
                          await websocket.send_text(error_payload)
                     except Exception as send_err:
                          logger.error(f"Failed to send error message over WebSocket: {send_err}")

    except WebSocketDisconnect:
        logger.info(f"WebSocket connection closed by client: {websocket.client.host}:{websocket.client.port}")
        # Client disconnected, ensure background tasks are cancelled

    except Exception as e:
        # Catch unexpected errors in the main WebSocket connection loop
        logger.error(f"Unexpected error in WebSocket endpoint: {e}", exc_info=True)
        # Attempt to close gracefully if possible
        if websocket.client_state == WebSocketState.CONNECTED:
             await websocket.close(code=1011) # Internal Error code

    finally:
        logger.info(f"Cleaning up WebSocket connection and associated tasks for: {websocket.client.host}:{websocket.client.port}")
        # Cancel all potentially running tasks associated with this connection
        tasks_to_cancel = [processing_task, owl_gen_task, log_sender_task]
        for task in tasks_to_cancel:
            if task and not task.done():
                logger.debug(f"Cancelling task {task.get_name()}...")
                task.cancel()
                try:
                    # Give task a moment to acknowledge cancellation
                    await asyncio.wait_for(task, timeout=1.0)
                except asyncio.CancelledError:
                    logger.debug(f"Task {task.get_name()} cancelled successfully.")
                except asyncio.TimeoutError:
                     logger.warning(f"Task {task.get_name()} did not cancel within timeout.")
                except Exception as e_cancel:
                     logger.error(f"Error cancelling task {task.get_name()}: {e_cancel}")
        logger.info("WebSocket cleanup complete.")


# Optional: Basic health check endpoint at the root
@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Provides basic API status information."""
    loaded_pipeline_path = os.getenv("PROCESSING_PIPELINE", "N/A")
    if processing_pipeline is not None:
        pipeline_name = processing_pipeline.__class__.__name__
        pipeline_status = f"Pipeline '{pipeline_name}' ({loaded_pipeline_path}) appears loaded."
        # Add more specific checks here if needed (e.g., model loaded status)
        # Example check:
        # if hasattr(processing_pipeline, 'is_ready') and callable(processing_pipeline.is_ready):
        #    pipeline_status += " Ready check: " + ("OK" if processing_pipeline.is_ready() else "Not Ready")
    else:
        pipeline_status = f"ERROR: Processing Pipeline ({loaded_pipeline_path}) FAILED to initialize. Check logs. Error: {pipeline_error}"

    return f"""
    <html><head><title>Oncology Processor API Status</title></head>
    <body>
        <h1>Oncology Phrase Processor API</h1>
        <p>Status: {pipeline_status}</p>
        <p>WebSocket connections handled at /ws (via proxy).</p>
    </body>
    </html>
    """

# --- END OF UPDATED api/main.py ---