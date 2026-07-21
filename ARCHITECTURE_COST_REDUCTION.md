# Architecture: lower cost without changing the assistant behavior

This project should keep the same user experience while reducing spend by moving cheap decisions earlier in the flow and reserving expensive calls for cases that actually need them.

## Current module roles

- `ui/main_window.py`
  - Owns the UI state, mode toggles, hotkeys, and the bridge to the runtime.
  - Starts conversations from text or voice.
  - Reflects state to Unreal and to the backend UI.

- `core/conversation_manager.py`
  - Orchestrates the turn lifecycle.
  - Owns `IDLE`, `LISTENING`, `THINKING`, and `TALKING`.
  - Coordinates OpenAI, TTS, playback, and runtime state updates.

- `core/openai_manager.py`
  - Generates the assistant response text.
  - Should stay the single place where model selection is resolved.

- `core/deepgram_stt_manager.py`
  - Only relevant when the user explicitly enables cloud STT.
  - Local STT stays the default path.

- `core/elevenlabs_manager.py`
  - Handles voice generation and streaming.
  - Should stay isolated from logic about when to speak.

- `core/usage_estimator.py`
  - Builds the visible usage snapshot.
  - Converts internal cost estimates into tokens and minutes.

- `core/mood.py`
  - Normalizes emotion values and maps moods to voice strength.

- `core/runtime_bridge.py`
  - Moves runtime state and audio metadata to Unreal.

## Decision flow

### 1. Input arrives

The assistant receives either:

- text input
- voice transcript input

The UI should not decide cost policy. It only forwards the input source.

### 2. Cheap routing first

Before the main model runs, a very cheap router decides:

- trivial greeting
- short command
- normal conversational turn
- long or complex turn
- voice-specific turn

The router should use heuristics or the cheapest available model.
It must not generate the final answer.

### 3. Context selection

Build the prompt from three buckets:

- recent turns
- running summary
- current user input

Context tiers:

- `min`: last few turns + summary
- `normal`: moderate history + summary
- `extended`: larger history only when needed

Default should be `normal`.
`extended` should be exceptional, not the baseline.

### 4. Main response generation

Use the selected OpenAI model only after the router and context selector decide it is justified.

Recommended model policy:

- fast/default model for most turns
- balanced model for richer turns
- quality model only when the user explicitly asks for better output or the turn is clearly high value

### 5. Post-processing

After the assistant text is generated:

- update mood
- update usage adaptation
- prepare TTS text
- decide whether the turn should be spoken with normal or expressive voice

### 6. TTS selection

TTS should be tiered, not uniform.

Policy:

- normal voice for most messages
- expressive voice only when:
  - the content is emotional
  - the content is long enough to justify it
  - the user is in a premium/high-quality path

The TTS engine stays the same; only the preset or model choice changes.

### 7. Playback and Unreal state

Only after audio is ready:

- send `thinking` while the assistant is still processing
- send `talking` shortly before audio starts
- keep the `talking` state aligned with actual audio playback

This prevents animation drift and avoids early talking states with no sound.

## Cost control layers

### STT

Default path:

- local STT

Optional path:

- cloud STT only when the user explicitly enables it

### Emotion analysis

Use the cheapest possible engine.

Recommended cadence:

- every 5 seconds
- only when enough words are present
- skip short greetings and low-value turns

### Context

Reduce the default prompt size.

Recommended policy:

- keep the last few turns
- keep a rolling summary
- avoid sending the entire chat history by default

### Response length

Default output should stay short.

The system should prefer:

- concise answers
- short completions
- no padding

### TTS

Use expressive voice only when it changes the perceived quality.

Do not spend expressive TTS on:

- one-word replies
- acknowledgements
- commands

## Suggested module split if this grows

If this needs to be separated further, the next clean split is:

- `core/request_router.py`
  - cheap turn classifier

- `core/context_builder.py`
  - history + summary assembly

- `core/emotion_analyzer.py`
  - emotion cadence and heuristics

- `core/voice_policy.py`
  - normal vs expressive TTS selection

This project does not need those files immediately if the current code remains readable, but those are the right boundaries.

## Practical rule set

1. Do not use the expensive model for trivial turns.
2. Do not analyze emotion unless enough text exists.
3. Do not send the full history unless the turn needs it.
4. Do not play expressive TTS unless the content justifies it.
5. Do not move state to `talking` before audio is actually ready.

## What this preserves

- same assistant behavior
- same voice workflow
- same Unreal integration
- same visible UX

What changes is the amount of money spent to produce that behavior.
