# FILE: api/main.py
# (REVISED - Removed OWL generation logic)

import asyncio
import json
import logging
import os
import importlib
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketState
from typing import Optional, List, Dict, Any, Tuple
from collections.abc import Callable
from dotenv import load_dotenv

# --- Pipeline Imports ---
try:
    from pipelines.base_pipeline import BaseProcessingPipeline
except ImportError as e:
    logging.basicConfig(level=logging.ERROR)
    logging.error(f"CRITICAL: Failed to import BaseProcessingPipeline: {e}", exc_info=True)
    # Define a dummy class if import fails, so the rest of the file can be parsed
    class BaseProcessingPipeline:
        def process(self, phrase: str, log_func: Optional[Callable[[str], None]] = None) -> Tuple[Dict[str, List[Dict[str, Any]]], str]:
            raise NotImplementedError("BaseProcessingPipeline could not be imported")

# --- Removed owl_generator Import ---
# from scripts.owl_generator import generate_owl

# --- Load Environment Variables ---
load_dotenv()

# --- Logging Setup ---
log_level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)
logging.basicConfig(level=log_level, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# --- Global Variables ---
processing_pipeline: Optional[BaseProcessingPipeline] = None
pipeline_error: Optional[str] = None

# --- Pipeline Loading Logic ---
def load_pipeline():
    """Dynamically loads and instantiates the processing pipeline specified in env vars."""
    global processing_pipeline, pipeline_error
    processing_pipeline = None
    pipeline_error = None
    pipeline_class_path = os.getenv("PROCESSING_PIPELINE")

    if not pipeline_class_path:
        pipeline_error = "PROCESSING_PIPELINE environment variable not set."
        logger.critical(pipeline_error)
        return

    logger.info(f"Attempting to load pipeline from path: {pipeline_class_path}")
    try:
        module_path, class_name = pipeline_class_path.rsplit('.', 1)
        PipelineModule = importlib.import_module(module_path)
        PipelineClass = getattr(PipelineModule, class_name)

        if not issubclass(PipelineClass, BaseProcessingPipeline):
             pipeline_error = f"Class '{class_name}' in '{module_path}' does not inherit from BaseProcessingPipeline."
             logger.critical(pipeline_error)
             return

        # Instantiate the pipeline (e.g., CTakesPipeline might take CTAKES_URL from env)
        processing_pipeline = PipelineClass()
        logger.info(f"Successfully loaded and instantiated pipeline: {pipeline_class_path}")

        # Check for process method
        if not hasattr(processing_pipeline, 'process') or not callable(processing_pipeline.process):
             pipeline_error = f"Pipeline class '{class_name}' does not have a callable 'process' method."
             logger.critical(pipeline_error)
             processing_pipeline = None # Invalidate pipeline
             return

    except ModuleNotFoundError:
        pipeline_error = f"Pipeline module not found: {module_path}"
        logger.critical(pipeline_error, exc_info=True)
    except AttributeError:
        pipeline_error = f"Pipeline class not found: {class_name} in {module_path}"
        logger.critical(pipeline_error, exc_info=True)
    except ImportError as e:
        # Catch import errors that might happen if a pipeline's dependencies are missing
        pipeline_error = f"Import error loading pipeline {pipeline_class_path}: {e}"
        logger.critical(pipeline_error, exc_info=True)
    except Exception as e:
        pipeline_error = f"Error instantiating pipeline {pipeline_class_path}: {e}"
        logger.exception("Pipeline instantiation failed")


# --- FastAPI App Setup ---
app = FastAPI(title="Oncology Phrase Processor", version="1.0.0")

@app.on_event("startup")
async def startup_event():
    logger.info("Application startup initiated...")
    load_pipeline() # Load the pipeline specified in env vars
    if processing_pipeline:
        logger.info("Processing pipeline loaded successfully during startup.")
    else:
        logger.error(f"CRITICAL: Processing pipeline failed to load during startup. Error: {pipeline_error}")
    logger.info("Application startup complete.")


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Application shutdown.")


# --- WebSocket Endpoint ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handles WebSocket connections for phrase processing and results."""

    # Check if pipeline loaded correctly during startup
    if processing_pipeline is None:
        logger.error("Pipeline not available, rejecting WebSocket connection.")
        await websocket.close(code=1011, reason=f"Server Error: Processing pipeline failed to load ({pipeline_error or 'Unknown reason'}).")
        return

    await websocket.accept()
    logger.info(f"WebSocket connection accepted from: {websocket.client.host}:{websocket.client.port}")

    # Setup for handling logs and tasks per connection
    log_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    log_sender_task: Optional[asyncio.Task] = None
    processing_task: Optional[asyncio.Task] = None
    # --- Removed owl_gen_task ---
    # owl_gen_task: Optional[asyncio.Task] = None
    main_event_loop = asyncio.get_running_loop()

    async def send_logs():
        """Task to continuously send logs from the queue to the client."""
        while True:
            try:
                message = await log_queue.get()
                if message is None: # Sentinel value to stop the task
                    log_queue.task_done()
                    break
                if websocket.client_state == WebSocketState.CONNECTED:
                    log_payload = json.dumps({"type": "log", "payload": message})
                    await websocket.send_text(log_payload)
                else:
                    logger.warning("WebSocket disconnected while trying to send log.")
                    log_queue.task_done()
                    break # Stop if disconnected
                log_queue.task_done()
            except asyncio.CancelledError:
                 logger.info("Log sending task cancelled.")
                 try: log_queue.task_done() # Ensure task_done is called even on cancellation
                 except ValueError: pass # Ignore if already done
                 break
            except Exception as e:
                 logger.error(f"Error in send_logs task: {e}", exc_info=True)
                 try: log_queue.task_done()
                 except ValueError: pass
                 break

    def log_callback_sync(message: str):
        """Thread-safe callback to put logs onto the async queue."""
        try:
            # Use call_soon_threadsafe to schedule put_nowait in the event loop
            main_event_loop.call_soon_threadsafe(log_queue.put_nowait, message)
        except Exception as e:
            # Log error if putting message fails (e.g., queue full, though unlikely with default size)
            logger.error(f"Error submitting log message to queue from thread: {e}")

    try:
        while True:
            data = await websocket.receive_text()
            logger.debug(f"Received raw message via WS: {data[:200]}...")

            try:
                message_data = json.loads(data)
                msg_type = message_data.get("type")
                payload = message_data.get("payload")

                if msg_type == "process_phrase" and isinstance(payload, str):
                    phrase = payload
                    logger.info(f"Processing request for phrase: '{phrase[:100]}...' using {processing_pipeline.__class__.__name__}")

                    # --- Cancel any previous running tasks for this connection ---
                    if processing_task and not processing_task.done(): processing_task.cancel()
                    # --- Removed owl_gen_task cancellation ---
                    # if owl_gen_task and not owl_gen_task.done(): owl_gen_task.cancel()
                    # Cancel and signal log sender to stop
                    if log_sender_task and not log_sender_task.done():
                        log_sender_task.cancel()
                        try: log_queue.put_nowait(None) # Send sentinel if possible
                        except asyncio.QueueFull: pass # Ignore if full, cancellation will handle it

                    # --- Clear log queue from previous run ---
                    while not log_queue.empty():
                         try: log_queue.get_nowait(); log_queue.task_done()
                         except (asyncio.QueueEmpty, ValueError): break

                    # --- Start new tasks for this request ---
                    log_sender_task = asyncio.create_task(send_logs(), name="LogSenderTask")
                    await websocket.send_json({"type": "status", "payload": "Processing started..."})

                    # Execute the pipeline processing in a separate thread
                    processing_task = asyncio.create_task(
                        asyncio.to_thread(
                            processing_pipeline.process, # Call the loaded pipeline's process method
                            phrase,
                            log_func=log_callback_sync # Pass the thread-safe logger
                        ),
                        name="PipelineProcessTask"
                    )

                    try:
                        # Await the result from the pipeline task
                        # Expecting a tuple: (json_results, raw_xmi_content)
                        pipeline_output: Tuple[Dict[str, List[Dict[str, Any]]], str] = await processing_task

                        # Check if the task was cancelled while waiting
                        if processing_task.cancelled():
                             logger.info("Processing task was cancelled before completion.")
                             await websocket.send_json({"type": "status", "payload": "Processing cancelled."})
                             continue # Wait for next message

                        # Unpack the results from the tuple
                        json_results, raw_xmi_content = pipeline_output

                        logger.info(f"Processing completed. Found {len(json_results.get('concepts',[]))} concepts, {len(json_results.get('relations',[]))} relations.")

                        # Construct the payload for the frontend including both parts
                        result_payload = {
                            "json_data": json_results,
                            "raw_xmi": raw_xmi_content
                        }

                        # Send the combined result payload
                        await websocket.send_json({"type": "result", "payload": result_payload})
                        logger.debug("Sent final JSON+XMI results via WS.")

                        # --- REMOVED OWL Generation Block ---

                        # Send final status after successful processing and result sending
                        await websocket.send_json({"type": "status", "payload": "Processing complete."})


                    except asyncio.CancelledError:
                         # This catches cancellation of the processing_task itself
                         logger.info("Processing task cancelled (likely by new request or disconnect).")
                         # Status message sent when cancellation is initiated or detected after await
                    except Exception as e:
                        # Catch errors during pipeline execution
                        logger.exception(f"Error during pipeline execution for '{phrase[:50]}...':")
                        if websocket.client_state == WebSocketState.CONNECTED:
                            # Send error details back to the client
                            error_payload = json.dumps({"type": "error", "payload": f"Processing failed: {e}"})
                            await websocket.send_text(error_payload)
                    finally:
                        # --- Ensure log sender task is stopped after processing (success or failure) ---
                        if log_sender_task and not log_sender_task.done():
                            logger.debug("Signalling log sender task to stop after request completion.")
                            try:
                                if not log_sender_task.cancelled():
                                    await log_queue.put(None) # Send sentinel value
                                # Wait briefly for the task to finish processing remaining logs
                                await asyncio.wait_for(log_sender_task, timeout=5.0)
                                logger.debug("Log sender task finished after request.")
                            except (asyncio.TimeoutError, asyncio.CancelledError):
                                logger.warning("Log sender task timed out or was cancelled while waiting.")
                                if not log_sender_task.done(): log_sender_task.cancel() # Force cancel if needed
                            except asyncio.QueueFull:
                                logger.warning("Log queue was full when trying to send sentinel.")
                                if not log_sender_task.done(): log_sender_task.cancel() # Force cancel
                            except Exception as e_log_wait:
                                logger.error(f"Error waiting for log sender task: {e_log_wait}")
                                if not log_sender_task.done(): log_sender_task.cancel() # Force cancel

                else:
                     # Handle invalid message types received from client
                     logger.warning(f"Received invalid message type/payload via WS: Type='{msg_type}', Payload Type='{type(payload)}'")
                     if websocket.client_state == WebSocketState.CONNECTED:
                          error_payload = json.dumps({"type": "error", "payload": "Invalid message format. Send {'type': 'process_phrase', 'payload': 'your text'}"})
                          await websocket.send_text(error_payload)

            except json.JSONDecodeError:
                 # Handle non-JSON messages
                 logger.warning(f"Received non-JSON message via WS: {data[:200]}")
                 if websocket.client_state == WebSocketState.CONNECTED:
                    error_payload = json.dumps({"type": "error", "payload": "Invalid JSON message received."})
                    await websocket.send_text(error_payload)
            except WebSocketDisconnect:
                 # Handle client disconnecting during message processing
                 logger.info(f"WebSocket disconnected during message processing: {websocket.client.host}:{websocket.client.port}")
                 break # Exit the while loop
            except Exception as e:
                 # Catch any other unexpected errors during message handling
                 logger.error(f"Unexpected error handling WebSocket message: {e}", exc_info=True)
                 if websocket.client_state == WebSocketState.CONNECTED:
                     try:
                          # Send a generic error message
                          error_payload = json.dumps({"type": "error", "payload": f"Internal server error: {e}"})
                          await websocket.send_text(error_payload)
                     except Exception as send_err:
                          logger.error(f"Failed to send error message over WebSocket: {send_err}")

    except WebSocketDisconnect:
        # Log when the client explicitly closes the connection
        logger.info(f"WebSocket connection closed by client: {websocket.client.host}:{websocket.client.port}")

    except Exception as e:
        # Catch unexpected errors in the main WebSocket endpoint loop
        logger.error(f"Unexpected error in WebSocket endpoint: {e}", exc_info=True)
        # Try to close the connection gracefully if an error occurs
        if websocket.client_state == WebSocketState.CONNECTED:
             await websocket.close(code=1011) # Internal Server Error code

    finally:
        # --- Final Cleanup ---
        # This block runs when the connection is closed (normally or due to error)
        logger.info(f"Cleaning up WebSocket connection and associated tasks for: {websocket.client.host}:{websocket.client.port}")
        # --- Removed owl_gen_task from cancellation list ---
        tasks_to_cancel = [processing_task, log_sender_task]
        for task in tasks_to_cancel:
            if task and not task.done():
                logger.debug(f"Cancelling task {task.get_name()}...")
                task.cancel()
                try:
                    # Wait briefly for task cancellation to complete
                    await asyncio.wait_for(task, timeout=1.0)
                except asyncio.CancelledError:
                    logger.debug(f"Task {task.get_name()} cancelled successfully.")
                except asyncio.TimeoutError:
                    logger.warning(f"Task {task.get_name()} did not cancel within timeout.")
                except Exception as e_cancel:
                    # Log errors during cancellation itself
                    logger.error(f"Error cancelling task {task.get_name()}: {e_cancel}")
        logger.info("WebSocket cleanup complete.")


# Optional: Basic health check endpoint / Root endpoint
@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Provides basic API status information."""
    loaded_pipeline_path = os.getenv("PROCESSING_PIPELINE", "N/A")
    # Check the global variables set during startup
    if processing_pipeline is not None:
        pipeline_name = processing_pipeline.__class__.__name__
        pipeline_status = f"Pipeline '{pipeline_name}' ({loaded_pipeline_path}) appears loaded."
    else:
        pipeline_status = f"ERROR: Processing Pipeline ({loaded_pipeline_path}) FAILED to initialize. Check logs. Error: {pipeline_error}"

    # Simple HTML response
    return f"""
    <html><head><title>Oncology Processor API Status</title></head>
    <body>
        <h1>Oncology Phrase Processor API</h1>
        <p>Status: {pipeline_status}</p>
        <p>WebSocket connections handled at /ws (via proxy).</p>
    </body>
    </html>
    """

# --- END OF api/main.py ---