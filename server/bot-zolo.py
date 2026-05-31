#
# Zolo — voice agent that operates your Mac by controlling the real cursor.
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Zolo — "Clicky points, Zolo clicks."

You speak; Zolo reads the screen (macOS Accessibility tree) and drives the real
cursor + keyboard to do the task. Same Pipecat voice loop as the flower starter,
with the mock catalog swapped for a desktop-control backend.

Pipeline: Nemotron Speech Streaming STT -> Nemotron-3-Super-120B LLM -> Gradium TTS,
with direct function tools (read_screen / click / type_text / press_key / scroll)
registered on the LLM context.

Run it locally (drives the real cursor):
    ENV=local ZOLO_MODE=live uv run bot-zolo.py
Then open http://localhost:7860, click Connect, switch to Safari, and talk.
"""

import asyncio
import os

from dotenv import load_dotenv
from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndTaskFrame, FunctionCallResultProperties, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.services.gradium.tts import GradiumTTSService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.daily.transport import DailyParams, DailyTransport
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_turn_strategies import FilterIncompleteUserTurnStrategies
from pipecat.workers.runner import WorkerRunner
from pipecatcloud.agent import DailySessionArguments

from desktop_control import DesktopController
from nemotron_llm import VLLMOpenAILLMService
from nvidia_stt import NVidiaWebSocketSTTService

load_dotenv(override=True)


# --- robustness wrappers (from plan-eng-review findings) ---------------------
# read_offloop: AX tree reads are blocking and can raise mid-walk if an element
# disappears. Run them off the event loop so audio never stalls, and never throw.
# act_safely: actuation (cursor moves, typing, scrolling) is blocking, so run it
# OFF the event loop too — a slow moveTo/type otherwise stalls STT/TTS and the
# voice session degrades (Codex #6). Never let an exception leave the LLM hanging.


async def read_offloop(read_callable):
    """Run a blocking screen read off the event loop; return an error dict, never raise."""
    try:
        return await asyncio.to_thread(read_callable)
    except Exception as exc:
        logger.exception("[zolo] screen read failed")
        return {
            "ok": False,
            "error": f"Couldn't read the screen ({type(exc).__name__}). Ask me to try again.",
        }


async def act_safely(action_callable, *args):
    """Run a blocking actuation call off the event loop; return an error dict, never raise."""
    try:
        return await asyncio.to_thread(action_callable, *args)
    except Exception as exc:
        logger.exception("[zolo] desktop action failed")
        return {
            "ok": False,
            "error": f"That action hit a snag ({type(exc).__name__}). Let me read the screen again.",
        }


async def run_bot(
    transport: BaseTransport,
    audio_in_sample_rate: int = 16000,
    audio_out_sample_rate: int = 24000,
):
    """Main bot logic.

    Args:
        transport: The transport to use.
        audio_in_sample_rate: Input audio sample rate in Hz. Defaults to 16000 (WebRTC).
        audio_out_sample_rate: Output audio sample rate in Hz. Defaults to 24000 (WebRTC).
    """
    logger.info("Starting Zolo")

    # Zolo's eyes + hands. live = real cursor (ENV=local); mock = canned screen +
    # logged actions (Cekura / cloud). See desktop_control.py.
    controller = DesktopController()

    # --- Tools the LLM can call ---------------------------------------------

    async def read_screen(params: FunctionCallParams) -> None:
        """Look at the FRONTMOST app and list the interactive elements you can act on
        (buttons, links, text fields, dropdowns), each with a short id and a label.

        Call this FIRST, before any click or type. Call it AGAIN after any action
        that changes what's on screen (navigation, a new form, a popup) OR after the
        user switches to a different app — element ids change, so an old id may be wrong."""
        await params.result_callback(await read_offloop(controller.read_screen))

    async def click(params: FunctionCallParams, target: str) -> None:
        """Click an element by its id (e.g. "e3") or its exact label (e.g.
        "Find a table"). The target must come from the most recent read_screen.

        This tool REFUSES committing controls (Book, Pay, Submit, Send, Delete,
        Place order) and tells you it needs confirmation. For those: tell the user
        what you're about to do, get a clear yes, then call confirm_and_click.

        Args:
            target: The id or label of the element to click.
        """
        await params.result_callback(await act_safely(controller.click, target))

    async def confirm_and_click(params: FunctionCallParams, target: str) -> None:
        """Click a COMMITTING control (Book / Pay / Submit / Send / Delete / Place order).
        Only call this AFTER you have told the user exactly what you're about to do AND they
        said yes in this conversation. Never call it on your own initiative.

        Args:
            target: The id or label of the committing element to click.
        """
        await params.result_callback(await act_safely(controller.confirm_click, target))

    async def type_text(params: FunctionCallParams, text: str) -> None:
        """Type text into the currently focused field. Click the field first so
        it's focused, then call this.

        Args:
            text: The literal text to type.
        """
        await params.result_callback(await act_safely(controller.type_text, text))

    async def press_key(params: FunctionCallParams, key: str) -> None:
        """Press a single special key, e.g. "return", "tab", "escape", "space".

        Args:
            key: The name of the key to press.
        """
        await params.result_callback(await act_safely(controller.press_key, key))

    async def scroll(params: FunctionCallParams, direction: str = "down", amount: int = 5) -> None:
        """Scroll the screen up or down to reveal more elements, then read_screen again.

        Args:
            direction: "up" or "down".
            amount: How far to scroll. Defaults to 5.
        """
        await params.result_callback(await act_safely(controller.scroll, direction, amount))

    async def do_actions(params: FunctionCallParams, steps: list) -> None:
        """Run several on-screen actions in ONE call, in order, for a multi-step task
        on the CURRENT screen (e.g. click a field, type text, press return). Prefer
        this over separate click/type calls so the task finishes in a single turn.

        Each step is an object:
          {"action": "click", "target": "<id or label>"}
          {"action": "type", "text": "<text to type>"}
          {"action": "press", "key": "<e.g. return, tab>"}
          {"action": "scroll", "direction": "up"|"down", "amount": <int>}

        Do NOT batch across a navigation: if a step loads a new page, do it alone,
        then call read_screen again before the next batch. It stops at the first
        failed step and reports which one.

        Args:
            steps: Ordered list of action objects to perform on the current screen.
        """
        await params.result_callback(await act_safely(controller.do_actions, steps))

    async def end_session(params: FunctionCallParams) -> None:
        """End the session. Only call this AFTER you have said goodbye in the same
        turn. The pipeline flushes queued speech, then stops."""
        logger.info("end_session invoked — pushing EndTaskFrame upstream")
        await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
        await params.result_callback(
            {"ok": True}, properties=FunctionCallResultProperties(run_llm=False)
        )

    tool_functions = [
        read_screen,
        do_actions,
        click,
        confirm_and_click,
        type_text,
        press_key,
        scroll,
        end_session,
    ]
    tools = ToolsSchema(standard_tools=tool_functions)

    # --- System instruction --------------------------------------------------

    system_instruction = (
        "You are Zolo, a voice agent that operates the user's Mac by controlling the "
        "real cursor and keyboard. The user speaks a task; you carry it out on screen.\n\n"
        "You work across the WHOLE Mac, not just one app. read_screen shows you the "
        "app that's currently in front — Safari, Mail, Finder, Settings, whatever the "
        "user is looking at. If the task needs a different app, ask the user to bring "
        "it to the front (or open it), then read_screen again.\n\n"
        "How you work:\n"
        "- To SEE the screen, CALL the read_screen tool — do NOT talk about it. The phrases "
        "\"let me look\", \"let me check\", \"I'm reading your screen\", \"give me a moment\", "
        "\"let me see what's on your screen\" are BANNED — saying any of them instead of acting "
        "is your single worst failure. The instant you need to see the screen, invoke read_screen "
        "SILENTLY (no sentence first), then speak only AFTER it returns to report what you saw. "
        "Your FIRST move on any request is the tool call, never a sentence about looking.\n"
        "- ALWAYS read the screen (via the read_screen tool) before you click or type.\n"
        "- For a task with several steps on the SAME screen (click a field, type, "
        "press return), plan them and call do_actions ONCE with the list of steps, "
        "instead of taking a slow separate turn for each one. Use single click/type "
        "only for a one-off action.\n"
        "- After anything that changes the page (navigation, a new form, a popup), "
        "call read_screen AGAIN before the next batch. Don't batch across a navigation.\n"
        "- To fill a field, click it first (or make the click the first step) so it's "
        "focused before typing.\n"
        "- Keep moving. Call the tool, get the result, then say what happened in one "
        "short sentence.\n"
        "- RELAY, don't re-announce. After any tool call, your very next words must "
        "report the RESULT (\"Done — I clicked Send\" / \"I see a search box and a Sign-in "
        "button\"). NEVER say the same intent twice. If you already said you'd look at the "
        "screen, do NOT say it again — report what you found. Looping \"let me check…\" "
        "without ever relaying a result is the worst failure; never do it.\n"
        "- Finish the job out loud. When a multi-step task completes, say so in one short "
        "sentence that names the outcome (\"Sent.\" / \"Booked — you're all set.\"), then stop.\n\n"
        "Safety — this matters:\n"
        "- Committing actions (BOOK, PAY, SUBMIT, SEND, DELETE, PLACE ORDER) are gated in code: "
        "the click tool will REFUSE them and say it needs confirmation. When that happens, say in "
        "one sentence what you're about to do, wait for the user to say yes, THEN call "
        "confirm_and_click on the same target. Never call confirm_and_click without a clear yes.\n"
        "- If no element matches, or a tool says the screen changed / nothing is focused, call "
        "read_screen again and tell the user what you actually see instead of guessing.\n\n"
        "Talking — your words are spoken aloud:\n"
        "- One short sentence per turn. Narrate as you act: \"Okay, clicking the search box.\"\n"
        "- Refer to things ONLY by their human label or what they do — \"the search box\", "
        "\"the blue Send button\", \"the Done button\". This is a hard rule with NO exceptions: "
        "NEVER speak element ids (e1, e2, e3), the word \"element\", tool names (read_screen, "
        "do_actions), pixel positions, coordinates, or any raw numbers from the screen data. "
        "Wrong: \"clicking element e3\" / \"the button at 420, 180\". Right: \"clicking the Send button\".\n"
        "- No bullet points, no emojis, no markdown. Use contractions. Skip filler like "
        "\"Absolutely!\" or \"Sure thing!\" — go straight to it.\n\n"
        "If the user just chats or asks what you can do, answer briefly and give an "
        "example: \"I can drive your browser by voice — try 'search for sushi near me.'\""
    )

    # Speech-to-Text — Nemotron Speech Streaming over WebSocket (16 kHz PCM mono).
    stt = NVidiaWebSocketSTTService(
        url=os.getenv("NVIDIA_ASR_URL", "ws://192.168.7.228:8081"),
        strip_interim_prefix=True,
    )

    # LLM — Nemotron-3-Super-120B via vLLM (OpenAI-compatible). Thinking OFF by
    # default for low voice latency; set NEMOTRON_ENABLE_THINKING=true to enable.
    enable_thinking = os.getenv("NEMOTRON_ENABLE_THINKING", "false").lower() == "true"
    llm = VLLMOpenAILLMService(
        api_key=os.getenv("NEMOTRON_LLM_API_KEY", "EMPTY"),
        base_url=os.getenv("NEMOTRON_LLM_URL", "http://192.168.7.228:8000/v1"),
        settings=VLLMOpenAILLMService.Settings(
            model=os.getenv("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super"),
            system_instruction=system_instruction,
            extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": enable_thinking}}},
        ),
    )

    # Text-to-Speech — Gradium.
    tts = GradiumTTSService(
        api_key=os.environ["GRADIUM_API_KEY"],
        settings=GradiumTTSService.Settings(
            voice=os.getenv("GRADIUM_VOICE_ID", "Eu9iL_CYe8N-Gkx_"),
        ),
    )

    # ToolsSchema describes the tools to the LLM; register_direct_function wires
    # the actual handlers. Both are required.
    for fn in tool_functions:
        llm.register_direct_function(fn)

    context = LLMContext(tools=tools)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # Noise-robust VAD: in a loud room the default VAD treats background
            # chatter as the user speaking and interrupts Zolo mid-sentence (sounds
            # "silent"). Require louder, more sustained speech to trigger a turn.
            # Tune via env for the room you're in.
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    confidence=float(os.getenv("ZOLO_VAD_CONFIDENCE", "0.85")),
                    start_secs=float(os.getenv("ZOLO_VAD_START_SECS", "0.3")),
                    stop_secs=float(os.getenv("ZOLO_VAD_STOP_SECS", "0.6")),
                    min_volume=float(os.getenv("ZOLO_VAD_MIN_VOLUME", "0.75")),
                )
            ),
            user_turn_strategies=FilterIncompleteUserTurnStrategies(),
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=audio_in_sample_rate,
            audio_out_sample_rate=audio_out_sample_rate,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        context.add_message(
            {
                "role": "user",
                "content": (
                    "The user just connected. Greet them in one short sentence: "
                    "\"Zolo here — tell me what to do on your screen.\""
                ),
            }
        )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Bot entry point. SmallWebRTC for the local demo; Daily for Cekura's cloud runs."""

    # Krisp noise filter is only available on Pipecat Cloud (ENV != local).
    if os.environ.get("ENV") != "local":
        from pipecat.audio.filters.krisp_viva_filter import KrispVivaFilter

        krisp_filter = KrispVivaFilter()
    else:
        krisp_filter = None

    match runner_args:
        case SmallWebRTCRunnerArguments():
            transport = SmallWebRTCTransport(
                webrtc_connection=runner_args.webrtc_connection,
                params=TransportParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                ),
            )
        case DailySessionArguments():
            # Pipecat Cloud — including Cekura's WebRTC test runs — backs each
            # session with a Daily room. Run mock mode here (no Mac screen on the
            # cloud container; desktop_control auto-selects mock when ENV != local).
            transport = DailyTransport(
                runner_args.room_url,
                runner_args.token,
                "Zolo",
                params=DailyParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                ),
            )
        case _:
            logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
            return

    await run_bot(transport)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
