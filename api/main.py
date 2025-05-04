# FILE: api/main.py
# (MODIFIED to send both JSON and Raw XMI in 'result' payload)

import asyncio
import json
import logging
import os
import importlib
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketState
from typing import Optional, List, Dict, Any, Tuple # <--- Added Tuple
from collections.abc import Callable
from dotenv import load_dotenv

# --- Pipeline Imports ---
try:
    from pipelines.base_pipeline import BaseProcessingPipeline
except ImportError as e:
    logging.basicConfig(level=logging.ERROR)
    logging.error(f"CRITICAL: Failed to import BaseProcessingPipeline: {e}", exc_info=True)
    class BaseProcessingPipeline: pass


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

        processing_pipeline = PipelineClass()
        logger.info(f"Successfully loaded and instantiated pipeline: {pipeline_class_path}")

        # --- START: Add check for process method signature (Optional but recommended) ---
        if not hasattr(processing_pipeline, 'process') or not callable(processing_pipeline.process):
             pipeline_error = f"Pipeline class '{class_name}' does not have a callable 'process' method."
             logger.critical(pipeline_error)
             processing_pipeline = None # Invalidate pipeline
             return
        # You might add more sophisticated checks on the expected return type if needed,
        # but Python's dynamic nature makes this complex. Rely on runtime behavior for now.
        # --- END: Add check for process method signature ---

    except ModuleNotFoundError:
        pipeline_error = f"Pipeline module not found: {module_path}"
        logger.critical(pipeline_error, exc_info=True)
    except AttributeError:
        pipeline_error = f"Pipeline class not found: {class_name} in {module_path}"
        logger.critical(pipeline_error, exc_info=True)
    except ImportError as e:
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
    load_pipeline()
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

    if processing_pipeline is None:
        logger.error("Pipeline not available, rejecting WebSocket connection.")
        await websocket.close(code=1011, reason=f"Server Error: Processing pipeline failed to load ({pipeline_error or 'Unknown reason'}).")
        return

    await websocket.accept()
    logger.info(f"WebSocket connection accepted from: {websocket.client.host}:{websocket.client.port}")

    log_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    log_sender_task: Optional[asyncio.Task] = None
    processing_task: Optional[asyncio.Task] = None
    owl_gen_task: Optional[asyncio.Task] = None
    main_event_loop = asyncio.get_running_loop()

    async def send_logs():
        """Task to continuously send logs from the queue to the client."""
        while True:
            try:
                message = await log_queue.get()
                if message is None: log_queue.task_done(); break
                if websocket.client_state == WebSocketState.CONNECTED:
                    log_payload = json.dumps({"type": "log", "payload": message})
                    await websocket.send_text(log_payload)
                else:
                    logger.warning("WebSocket disconnected while trying to send log.")
                    log_queue.task_done(); break
                log_queue.task_done()
            except asyncio.CancelledError:
                 logger.info("Log sending task cancelled.")
                 try: log_queue.task_done()
                 except ValueError: pass
                 break
            except Exception as e:
                 logger.error(f"Error in send_logs task: {e}", exc_info=True)
                 try: log_queue.task_done()
                 except ValueError: pass
                 break

    def log_callback_sync(message: str):
        """Thread-safe callback to put logs onto the async queue."""
        try:
            main_event_loop.call_soon_threadsafe(log_queue.put_nowait, message)
        except Exception as e:
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

                    # Cancel previous tasks
                    if processing_task and not processing_task.done(): processing_task.cancel()
                    if owl_gen_task and not owl_gen_task.done(): owl_gen_task.cancel()
                    if log_sender_task and not log_sender_task.done():
                        log_sender_task.cancel()
                        try: log_queue.put_nowait(None)
                        except asyncio.QueueFull: pass

                    # Clear log queue
                    while not log_queue.empty():
                         try: log_queue.get_nowait(); log_queue.task_done()
                         except (asyncio.QueueEmpty, ValueError): break

                    # Start new tasks
                    log_sender_task = asyncio.create_task(send_logs(), name="LogSenderTask")
                    await websocket.send_json({"type": "status", "payload": "Processing started..."})

                    # Execute pipeline in thread
                    processing_task = asyncio.create_task(
                        asyncio.to_thread(
                            processing_pipeline.process,
                            phrase,
                            log_func=log_callback_sync
                        ),
                        name="PipelineProcessTask"
                    )

                    try:
                        # --- START: Modified result handling ---
                        # Expect a tuple: (json_results, raw_xmi_content)
                        pipeline_output: Tuple[Dict[str, List[Dict[str, Any]]], str] = await processing_task

                        # Check for cancellation *after* await
                        if processing_task.cancelled():
                             logger.info("Processing task was cancelled before completion.")
                             await websocket.send_json({"type": "status", "payload": "Processing cancelled."})
                             continue

                        # Unpack the results
                        json_results, raw_xmi_content = pipeline_output

                        logger.info(f"Processing completed. Found {len(json_results.get('concepts',[]))} concepts, {len(json_results.get('relations',[]))} relations.")

                        # Construct the payload for the frontend
                        result_payload = {
                            "json_data": json_results,
                            "raw_xmi": raw_xmi_content
                        }

                        # Send the combined result
                        await websocket.send_json({"type": "result", "payload": result_payload})
                        logger.debug("Sent final JSON+XMI results via WS.")
                        # --- END: Modified result handling ---

                        # --- Trigger OWL Generation (using json_results) ---
                        await websocket.send_json({"type": "status", "payload": "Generating OWL..."})
                        try:
                            from scripts.owl_generator import generate_owl
                            # Use the unpacked json_results here
                            concepts = json_results.get('concepts', [])
                            relations = json_results.get('relations', [])

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
                    except Exception as e:
                        logger.exception(f"Error during pipeline execution for '{phrase[:50]}...':")
                        if websocket.client_state == WebSocketState.CONNECTED:
                            error_payload = json.dumps({"type": "error", "payload": f"Processing failed: {e}"})
                            await websocket.send_text(error_payload)
                    finally:
                        # Signal and wait for log sender task
                        if log_sender_task and not log_sender_task.done():
                            logger.debug("Signalling log sender task to stop after request completion.")
                            try:
                                if not log_sender_task.cancelled(): await log_queue.put(None)
                                await asyncio.wait_for(log_sender_task, timeout=5.0)
                                logger.debug("Log sender task finished after request.")
                            except (asyncio.TimeoutError, asyncio.CancelledError):
                                logger.warning("Log sender task timed out or was cancelled while waiting.")
                                if not log_sender_task.done(): log_sender_task.cancel()
                            except asyncio.QueueFull:
                                logger.warning("Log queue was full when trying to send sentinel.")
                                if not log_sender_task.done(): log_sender_task.cancel()
                            except Exception as e_log_wait:
                                logger.error(f"Error waiting for log sender task: {e_log_wait}")
                                if not log_sender_task.done(): log_sender_task.cancel()

                else:
                     logger.warning(f"Received invalid message type/payload via WS: Type='{msg_type}', Payload Type='{type(payload)}'")
                     if websocket.client_state == WebSocketState.CONNECTED:
                          error_payload = json.dumps({"type": "error", "payload": "Invalid message format. Send {'type': 'process_phrase', 'payload': 'your text'}"})
                          await websocket.send_text(error_payload)

            except json.JSONDecodeError:
                 logger.warning(f"Received non-JSON message via WS: {data[:200]}")
                 if websocket.client_state == WebSocketState.CONNECTED:
                    error_payload = json.dumps({"type": "error", "payload": "Invalid JSON message received."})
                    await websocket.send_text(error_payload)
            except WebSocketDisconnect:
                 logger.info(f"WebSocket disconnected during message processing: {websocket.client.host}:{websocket.client.port}")
                 break
            except Exception as e:
                 logger.error(f"Unexpected error handling WebSocket message: {e}", exc_info=True)
                 if websocket.client_state == WebSocketState.CONNECTED:
                     try:
                          error_payload = json.dumps({"type": "error", "payload": f"Internal server error: {e}"})
                          await websocket.send_text(error_payload)
                     except Exception as send_err:
                          logger.error(f"Failed to send error message over WebSocket: {send_err}")

    except WebSocketDisconnect:
        logger.info(f"WebSocket connection closed by client: {websocket.client.host}:{websocket.client.port}")

    except Exception as e:
        logger.error(f"Unexpected error in WebSocket endpoint: {e}", exc_info=True)
        if websocket.client_state == WebSocketState.CONNECTED:
             await websocket.close(code=1011)

    finally:
        logger.info(f"Cleaning up WebSocket connection and associated tasks for: {websocket.client.host}:{websocket.client.port}")
        tasks_to_cancel = [processing_task, owl_gen_task, log_sender_task]
        for task in tasks_to_cancel:
            if task and not task.done():
                logger.debug(f"Cancelling task {task.get_name()}...")
                task.cancel()
                try: await asyncio.wait_for(task, timeout=1.0)
                except asyncio.CancelledError: logger.debug(f"Task {task.get_name()} cancelled successfully.")
                except asyncio.TimeoutError: logger.warning(f"Task {task.get_name()} did not cancel within timeout.")
                except Exception as e_cancel: logger.error(f"Error cancelling task {task.get_name()}: {e_cancel}")
        logger.info("WebSocket cleanup complete.")


# Optional: Basic health check endpoint
@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Provides basic API status information."""
    loaded_pipeline_path = os.getenv("PROCESSING_PIPELINE", "N/A")
    if processing_pipeline is not None:
        pipeline_name = processing_pipeline.__class__.__name__
        pipeline_status = f"Pipeline '{pipeline_name}' ({loaded_pipeline_path}) appears loaded."
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