# Antigravity/Gemini Rules for TeleCLI

These rules govern the development and behavior of the Antigravity/Gemini AI agent within the TeleCLI project. They are derived from project documentation, including WARP.md, KIRO_INSTRUCTIONS.md, and Copilot instructions.

## 1. Core Development Principles
- **Follow Specifications**: Adhere strictly to the architecture and rules defined in `WARP.md` and `docs/session-fixes/`.
- **Modular Design**: Keep code modular and placed in the appropriate modules under `src/`.
- **Async First**: All web endpoints and terminal operations must be non-blocking and use `asyncio`.

## 2. Git & Workflow Rules (CRITICAL)
- **NO MAIN COMMITS**: Never commit directly to the `main` branch.
- **Feature Branches**: Always work on branches named `feature/description` or `fix/description`.
- **Immediate Branching**: Create the feature branch as soon as a task is assigned.
- **Frequent Commits**: Commit logical units of work as they are completed.
- **Conventional Commits**: Use clear, descriptive messages (e.g., `feat:`, `fix:`, `docs:`, `refactor:`).
- **PR Workflow**: Create Pull Requests only after the user validates the work.

## 3. Configuration Management
- **Environment Variables**: All settings must be externalized to `.env`.
- **No Hardcoding**: Never hardcode paths, API tokens, sensitive data, or configuration values.
- **Config Loader**: Use `src/config.py` for all configuration loading.
- **Defaults**: Provide sensible defaults in `config.py` and document them in `.env.sample`.

## 4. Logging Standards
- **Standard Logger**: Use `logger = logging.getLogger(__name__)` in every module.
- **No print()**: Never use the `print()` function for logging or debug information.
- **Appropriate Levels**:
  - `DEBUG`: Detailed internal flow and state.
  - `INFO`: User actions and significant lifecycle events.
  - `WARNING`: Recoverable issues or important notices.
  - `ERROR`: Failures requiring attention (log before raising).
- **Format Consistency**: Follow the format `[TIMESTAMP] [LEVEL] [MODULE] message`.
- **Interactions**: Log LLM interactions to `logs/llm_interactions.log` using the dedicated `llm_logger`.

## 5. Terminal & Session Management
- **Terminal Wrapper**: Use the pexpect-based `TerminalWrapper` for all shell interactions.
- **Isolation**: Ensure each user/client has a completely isolated terminal session.
- **Lifecycle**: Properly initialize sessions on start and clean up all resources on close/timeout.
- **Resizing**: Handle terminal resize events to ensure correct rendering.

## 6. AI Proxy & LLM System
- **Provider Interface**: New providers must implement the `LLMProvider` base class.
- **Fallback Logic**: Respect the intelligent fallback system (Gemini -> Claude -> GitHub) on 429 rate limits.
- **Prompt Engineering**: Use the JSON-based response format for AI Proxy interactions as defined in `src/ai_proxy.py`.
- **Memory Compression**: Implement and respect memory summarization to stay within context limits.

## 7. Quality & Testing
- **Pre-commit Testing**: Run `pytest tests/` before considering any task complete.
- **Verification**: Manually verify changes on both Web and Telegram interfaces if applicable.
- **Documentation**: Update the `docs/session-fixes/` index and specific fix files when adding functionality.

## 8. Security
- **Auth**: Respect `AUTH_REQUIRED` and `AUTH_TOKEN` settings.
- **Whitelisting**: Ensure command execution respects `ALLOWED_COMMANDS_ONLY` when enabled.
- **Sanitization**: Sanitize all terminal output before sending to LLMs or UI.
