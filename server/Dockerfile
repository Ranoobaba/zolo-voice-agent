FROM dailyco/pipecat-base:latest

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Uncomment if you wish to print a summary of the features available in the base image
# ENV PCC_LOG_FEATURES_SUMMARY=true

# Install the project's dependencies using the lockfile and settings
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Copy the application code.
# Pipecat Cloud's base image runs `bot.py` as the entrypoint, so the Zolo bot is
# copied in under that name. It imports the local modules below. On this Linux
# cloud container, desktop_control auto-selects mock mode (ENV != local), so the
# macOS-only pyobjc/pyautogui deps are neither needed nor installed here.
COPY ./bot-zolo.py bot.py
COPY ./desktop_control.py desktop_control.py
COPY ./nemotron_llm.py nemotron_llm.py
COPY ./nvidia_stt.py nvidia_stt.py