# Shell Agent Safety and Test Pass

- Date: 2026-07-02
- Status: approved for implementation

## Goal

Turn the current MVP into a safer baseline for continued iteration. This pass does not add the Skill engine or new executors. It tightens the existing CLI/Web/SSH/LLM flow and adds repeatable offline tests.

## Scope

1. Add a pytest-based test suite for command parsing, input classification, LLM response parsing, audit storage, and Web API behavior.
2. Make Web direct commands follow the same preview-and-confirm behavior as LLM-generated commands.
3. Prevent `/api/config` from returning stored API keys in plaintext.
4. Document Python 3.11+ setup and test commands.

## Non-Goals

- No real SSH integration tests.
- No live OpenAI or compatible LLM calls.
- No authentication layer in this pass.
- No Skill engine implementation in this pass.

## Validation

All tests must run locally without network, real servers, or secrets. The WebSocket command test verifies that direct commands do not execute until a confirm message is handled.
